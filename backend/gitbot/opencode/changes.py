"""Sandboxed code-change execution against a disposable working copy.

This is the only write path in :class:`gitbot.tasks.engine.TaskEngine`.

* ``git archive HEAD`` produces an immutable snapshot; the source repository is
  never mounted, so the model cannot see its ``.git`` directory or write to it.
* the snapshot is copied into a private ``/work/repo`` tree that is mounted
  read-write. That tree has no ``.git`` directory.
* a separate bare git repository lives *outside* the sandbox mount and records
  the baseline. The model cannot rewrite baseline refs or the index to hide a
  change.
* the review patch is generated from that host-side baseline after the sandbox
  exits, then stored and hashed for the existing approval flow.
* a sandbox/policy failure raises; there is intentionally no live-repository
  fallback.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gitbot.errors import PolicyViolation
from gitbot.gitops.repo import DEFAULT_DIFF_LIMIT
from gitbot.opencode.adapter import (
    OpenCodeConfig,
    RunClassification,
    build_run_argv,
    classify_stream,
)
from gitbot.opencode.gateway import (
    GatewayConfig,
    ai_gateway_session,
    classify_provider_error,
)
from gitbot.opencode.policy import PermissionRule
from gitbot.opencode.sandbox_policy import install_policy, restore_policy, verify_policy
from gitbot.security import redact_text
from gitbot.security.allowlist import PROJECT_ROOT
from gitbot.security.sandbox import (
    SANDBOX_WORK,
    SnapshotError,
    build_sandbox_command,
    isolated_repository,
    run_sandbox_command,
)

SANDBOX_WORK_REPO = f"{SANDBOX_WORK}/repo"
TEST_OUTPUT_LIMIT = 20_000
_SHELL_METACHARACTERS = frozenset({"&&", "||", ";", "|", ">", "<", ">>", "2>", "2>&1"})

# The sandbox is the security boundary. These rules stop the model from asking
# OpenCode to run shell commands or external tools; edits are only possible
# through the file tools inside the disposable /work tree. The disposable tree
# deliberately has no .git directory, so Git commands would not be useful there.
WRITE_RULES: tuple[PermissionRule, ...] = (
    PermissionRule("read", "*", "allow"),
    PermissionRule("edit", "*", "allow"),
    PermissionRule("write", "*", "allow"),
    PermissionRule("shell", "*", "deny"),
    PermissionRule("execute", "*", "deny"),
)

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/false",
    "HOME": "/nonexistent",
}
_GIT_SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
    "-c",
    "core.pager=cat",
    "-c",
    "color.ui=false",
)
_GIT_AUTHOR = (
    "-c",
    "user.name=GitBot Sandbox",
    "-c",
    "user.email=sandbox@gitbot.invalid",
    "-c",
    "commit.gpgsign=false",
)


@dataclass(frozen=True, slots=True)
class StagedDiff:
    patch: str
    files: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ChangeResult:
    classification: RunClassification
    snapshot_head: str
    duration_ms: int
    patch: str = ""
    files: tuple[str, ...] = ()
    diff_truncated: bool = False
    output: str | None = None
    output_truncated: bool = False
    session_id: str | None = None
    error_message: str | None = None
    denied_actions: list[dict] | None = None
    tests_status: str = "not_run"
    tests_output: str | None = None
    tests_output_truncated: bool = False
    tests_command: str | None = None
    test_exit_code: int | None = None


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    timeout_s: float = 60.0,
) -> str:
    argv = ["git", *_GIT_SAFE_CONFIG]
    if git_dir is not None:
        argv.extend(["--git-dir", str(git_dir)])
    if work_tree is not None:
        argv.extend(["--work-tree", str(work_tree)])
    argv.extend(args)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_GIT_ENV,
        )
    except FileNotFoundError as exc:
        raise SnapshotError("git executable not found while preparing the sandbox") from exc
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError("git command timed out while preparing the sandbox") from exc
    if completed.returncode != 0:
        raise SnapshotError(
            f"git {args[0]} failed while preparing the sandbox workspace",
            details={
                "exit_code": completed.returncode,
                "stderr": redact_text(completed.stderr or "")[-800:],
            },
        )
    return completed.stdout or ""


def _copy_snapshot(snapshot_root: Path, workspace_root: Path) -> Path:
    """Copy the extracted snapshot into the private writable workspace."""
    target = Path(workspace_root) / "repo"
    if target.exists():
        raise PolicyViolation("sandbox workspace already contains a repository copy")
    try:
        shutil.copytree(snapshot_root, target, copy_function=shutil.copyfile)
        for path in target.rglob("*"):
            if path.is_file():
                # The snapshot is read-only; restore write permission while
                # preserving any executable bits from the original tree.
                path.chmod(0o644 | (path.stat().st_mode & 0o111))
    except OSError as exc:
        raise PolicyViolation("could not create the writable sandbox workspace") from exc
    return target


def _init_baseline(work_tree: Path, git_dir: Path) -> None:
    """Create the host-only baseline repository that the model cannot reach."""
    _run_git(["init", "--bare", "--quiet", "-b", "main", str(git_dir)], cwd=work_tree)
    _run_git(["add", "-A", "-f"], cwd=work_tree, git_dir=git_dir, work_tree=work_tree)
    _run_git(
        [*_GIT_AUTHOR, "commit", "--quiet", "-m", "gitbot sandbox baseline"],
        cwd=work_tree,
        git_dir=git_dir,
        work_tree=work_tree,
    )


def capture_staged_diff(
    repo: Path | str,
    *,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    stage: bool = False,
    max_chars: int = DEFAULT_DIFF_LIMIT,
) -> StagedDiff:
    """Return the exact index-vs-HEAD patch, including binary changes.

    ``stage`` is used inside the disposable workspace; the live commit path has
    already applied the patch to the index and must not run ``git add`` again.
    """
    root = Path(repo)
    kwargs = {"cwd": root, "git_dir": git_dir, "work_tree": work_tree}
    if stage:
        _run_git(["add", "-A", "-f"], **kwargs)
    patch = _run_git(
        [
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
        ],
        **kwargs,
    )
    files = tuple(
        line
        for line in _run_git(
            ["diff", "--cached", "--name-only", "--no-color"],
            **kwargs,
        ).splitlines()
        if line
    )
    _reject_special_entries(root, files=files, git_dir=git_dir, work_tree=work_tree)
    truncated = len(patch) > max_chars
    return StagedDiff(patch=patch[:max_chars], files=files, truncated=truncated)


def _reject_special_entries(
    repo: Path,
    *,
    files: tuple[str, ...],
    git_dir: Path | None,
    work_tree: Path | None,
) -> None:
    """Symlinks and submodules can reach outside the reviewable repository."""
    if not files:
        return
    raw = _run_git(
        ["ls-files", "-s", "-z"],
        cwd=repo,
        git_dir=git_dir,
        work_tree=work_tree,
    )
    changed = set(files)
    special: list[str] = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if path not in changed:
            continue
        mode = meta.split(" ", 1)[0] if meta else ""
        if mode in {"120000", "160000"}:
            special.append(path)
    if special:
        raise PolicyViolation(
            "generated diff contains a symlink or submodule and was refused",
            details={"paths": sorted(special)[:20]},
        )


def _prepare_review_repo(snapshot_root: Path, workspace_root: Path) -> tuple[Path, Path]:
    work_tree = _copy_snapshot(snapshot_root, workspace_root)
    git_dir = Path(snapshot_root).parent / "review.git"
    _init_baseline(work_tree, git_dir)
    return work_tree, git_dir


@dataclass(frozen=True, slots=True)
class TestResult:
    status: str = "not_run"
    output: str | None = None
    output_truncated: bool = False
    command: str | None = None
    exit_code: int | None = None


def _parse_test_command(command: str) -> list[str]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise PolicyViolation(f"test command could not be parsed: {exc}") from exc
    if not argv:
        raise PolicyViolation("test command is empty")
    if any(token in _SHELL_METACHARACTERS for token in argv):
        raise PolicyViolation(
            "test command must be a single argv command; shell operators are not supported"
        )
    return argv


def _run_tests(
    *,
    snapshot_root: Path,
    workspace_root: Path,
    test_command: str,
    bwrap: str,
    timeout_s: float,
    cancel_event: threading.Event | None,
    kill_grace_s: float,
    max_output_chars: int,
) -> TestResult:
    """Run the explicitly configured test command inside the same sandbox.

    The command is chosen by the user in repository settings, never supplied by
    repository content or the model. It is split with ``shlex`` and executed as
    a fixed argv (no shell) in the disposable workspace after the model run.
    """
    try:
        argv = _parse_test_command(test_command)
        spec = build_sandbox_command(
            snapshot_root,
            workspace_root,
            argv,
            bwrap=bwrap,
            cwd=SANDBOX_WORK_REPO,
        )
        run = run_sandbox_command(
            spec,
            timeout_s=timeout_s,
            cancel_event=cancel_event,
            kill_grace_s=kill_grace_s,
        )
    except PolicyViolation as exc:
        return TestResult(
            status="error",
            output=redact_text(f"could not start the test command: {exc.message}"),
            command=test_command,
        )

    combined = "\n".join(part for part in (run.stdout.strip(), run.stderr.strip()) if part).strip()
    bounded = redact_text(combined)[:max_output_chars]
    truncated = run.stdout_truncated or run.stderr_truncated or len(combined) > max_output_chars
    if run.cancelled or run.timed_out:
        status = "error"
    elif run.exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    return TestResult(
        status=status,
        output=bounded or None,
        output_truncated=truncated,
        command=test_command,
        exit_code=run.exit_code,
    )


def run_change(
    source: Path | str,
    prompt: str,
    *,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (PROJECT_ROOT,),
    config: OpenCodeConfig | None = None,
    timeout_s: float | None = None,
    cancel_event: threading.Event | None = None,
    temporary_root: Path | str | None = None,
    bwrap: str = "bwrap",
    require_clean: bool = True,
    test_command: str | None = None,
    gateway: GatewayConfig | None = None,
) -> ChangeResult:
    """Run one code-change task in a sandbox and return its generated diff."""
    effective_config = config or OpenCodeConfig()
    effective_timeout = effective_config.timeout_s if timeout_s is None else timeout_s

    with ai_gateway_session(gateway, bwrap=bwrap, cancel_event=cancel_event) as endpoint:
        command = build_run_argv(effective_config, prompt)
        ro_binds: tuple[tuple[Path, str], ...] = ()
        rw_binds: tuple[tuple[Path, str], ...] = ()
        gateway_policy = None
        if endpoint is not None:
            command, ro_binds = endpoint.prepare(
                command,
                executable=effective_config.executable,
            )
            rw_binds = endpoint.rw_binds
            gateway_policy = endpoint.policy

        with isolated_repository(
            source,
            allowed_roots=allowed_roots,
            forbidden_roots=forbidden_roots,
            command=command,
            extra_ro_binds=ro_binds,
            extra_rw_binds=rw_binds,
            temporary_root=temporary_root,
            bwrap=bwrap,
            require_clean=require_clean,
            cwd=SANDBOX_WORK_REPO,
        ) as sandbox:
            work_tree, git_dir = _prepare_review_repo(sandbox.snapshot.root, sandbox.workspace.root)
            original_policy = install_policy(work_tree, WRITE_RULES, gateway=gateway_policy)
            try:
                verify_policy(
                    sandbox.snapshot.root,
                    sandbox.workspace.root,
                    effective_config,
                    rules=WRITE_RULES,
                    gateway=gateway_policy,
                    sandbox_cwd=SANDBOX_WORK_REPO,
                    bwrap=bwrap,
                )
                if cancel_event is not None and cancel_event.is_set():
                    return ChangeResult(
                        classification=RunClassification.CANCELLED,
                        snapshot_head=sandbox.snapshot.head,
                        duration_ms=0,
                        error_message="cancelled",
                    )
                run = run_sandbox_command(
                    sandbox.command,
                    timeout_s=effective_timeout,
                    cancel_event=cancel_event,
                    kill_grace_s=effective_config.kill_grace_s,
                )
            finally:
                restore_policy(work_tree, original_policy)

            cancelled_requested = cancel_event is not None and cancel_event.is_set()
            classified = classify_stream(
                run.stdout,
                run.stderr,
                exit_code=run.exit_code,
                timed_out=run.timed_out,
                cancelled=run.cancelled or cancelled_requested,
                max_output_chars=effective_config.max_output_chars,
            )
            if classified.classification in (
                RunClassification.PROCESS_ERROR,
                RunClassification.PROTOCOL_ERROR,
            ):
                provider_error = classify_provider_error(classified.error_message or "")
                if provider_error is not None:
                    raise provider_error
            diff = StagedDiff(patch="", files=(), truncated=False)
            tests = TestResult()
            if classified.classification is RunClassification.SUCCESS:
                # A cancellation that arrives after the model process exits but
                # before the task is stored must still cancel the task. Otherwise
                # the UI would report "cancel requested" while the task silently
                # moved into review with an approval.
                if cancel_event is not None and cancel_event.is_set():
                    return ChangeResult(
                        classification=RunClassification.CANCELLED,
                        snapshot_head=sandbox.snapshot.head,
                        duration_ms=run.duration_ms,
                        output=classified.output,
                        output_truncated=classified.output_truncated,
                        session_id=classified.session_id,
                        error_message="cancelled",
                    )
                # Capture the model's change before tests run, so test caches or
                # generated artifacts are never included in the reviewed diff.
                diff = capture_staged_diff(
                    work_tree,
                    git_dir=git_dir,
                    work_tree=work_tree,
                    stage=True,
                )
                configured = (test_command or "").strip()
                if configured:
                    tests = _run_tests(
                        snapshot_root=sandbox.snapshot.root,
                        workspace_root=sandbox.workspace.root,
                        test_command=configured,
                        bwrap=bwrap,
                        timeout_s=effective_timeout,
                        cancel_event=cancel_event,
                        kill_grace_s=effective_config.kill_grace_s,
                        max_output_chars=effective_config.max_output_chars,
                    )
                if cancel_event is not None and cancel_event.is_set():
                    return ChangeResult(
                        classification=RunClassification.CANCELLED,
                        snapshot_head=sandbox.snapshot.head,
                        duration_ms=run.duration_ms,
                        output=classified.output,
                        output_truncated=classified.output_truncated,
                        session_id=classified.session_id,
                        error_message="cancelled",
                    )
            return ChangeResult(
                classification=classified.classification,
                snapshot_head=sandbox.snapshot.head,
                duration_ms=run.duration_ms,
                patch=diff.patch,
                files=diff.files,
                diff_truncated=diff.truncated,
                output=classified.output,
                output_truncated=classified.output_truncated,
                session_id=classified.session_id,
                error_message=classified.error_message,
                denied_actions=classified.denied_actions,
                tests_status=tests.status,
                tests_output=tests.output,
                tests_output_truncated=tests.output_truncated,
                tests_command=tests.command,
                test_exit_code=tests.exit_code,
            )
