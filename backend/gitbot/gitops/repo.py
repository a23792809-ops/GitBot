"""Read-only git inspection for allowlisted repositories.

Phase 2b deliberately exposes no mutating operation: no checkout, no branch
creation, no commit, no push. ``preflight`` only *reports* what Phase 3 would do.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gitbot.errors import GitBotError, UpstreamError, UpstreamTimeout
from gitbot.security import redact_text

DEFAULT_DIFF_LIMIT = 256 * 1024  # characters of patch text
BRANCH_PREFIX = "gitbot"


class NotAGitRepository(GitBotError):
    code = "not_a_git_repository"
    http_status = 400


class GitCommandError(UpstreamError):
    code = "git_command_error"


@dataclass(frozen=True, slots=True)
class GitStatus:
    branch: str | None
    head: str | None
    upstream: str | None
    ahead: int
    behind: int
    clean: bool
    changed_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def detached(self) -> bool:
        return self.branch is None

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head": self.head,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "clean": self.clean,
            "detached": self.detached,
            "changed_files": list(self.changed_files),
        }


@dataclass(frozen=True, slots=True)
class GitDiff:
    patch: str
    files: tuple[str, ...]
    truncated: bool
    staged: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "patch": self.patch,
            "files": list(self.files),
            "truncated": self.truncated,
            "staged": self.staged,
        }


@dataclass(frozen=True, slots=True)
class Preflight:
    ok: bool
    reasons: tuple[str, ...]
    status: GitStatus
    suggested_branch: str

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "branch": self.status.branch,
            "clean": self.status.clean,
            "suggested_branch": self.suggested_branch,
            "states_only": "no working tree changes were made",
        }


def _git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = "true"
    environment["GIT_PAGER"] = "cat"
    environment["LC_ALL"] = "C"
    return environment


class GitRepo:
    def __init__(
        self,
        path: Path | str,
        *,
        executable: str = "git",
        timeout_s: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.executable = executable
        self.timeout_s = timeout_s
        self._env = _git_env(env)

    # -- plumbing --------------------------------------------------------
    def run(self, args: Sequence[str], *, timeout_s: float | None = None) -> str:
        argv = [self.executable, *args]
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                cwd=self.path,
                capture_output=True,
                text=True,
                timeout=timeout_s if timeout_s is not None else self.timeout_s,
                env=self._env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitCommandError(
                f"{self.executable!r} was not found on PATH",
                details={"executable": self.executable},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise UpstreamTimeout(
                "git command timed out", details={"argv": list(args[:3])}
            ) from exc

        if completed.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args[:2])} failed with exit code {completed.returncode}",
                details={
                    "command": " ".join(args[:2]),
                    "exit_code": completed.returncode,
                    "stderr": redact_text(completed.stderr or "")[-800:],
                },
            )
        return completed.stdout or ""

    # -- reads -----------------------------------------------------------
    def status(self) -> GitStatus:
        output = self.run(["status", "--porcelain=v2", "--branch", "-z"])
        branch: str | None = None
        head: str | None = None
        upstream: str | None = None
        ahead = 0
        behind = 0
        changed: list[str] = []

        for entry in output.split("\0"):
            if not entry:
                continue
            if entry.startswith("# branch.head "):
                value = entry[len("# branch.head ") :]
                branch = None if value == "(detached)" else value
            elif entry.startswith("# branch.oid "):
                value = entry[len("# branch.oid ") :]
                head = None if value == "(initial)" else value
            elif entry.startswith("# branch.upstream "):
                upstream = entry[len("# branch.upstream ") :]
            elif entry.startswith("# branch.ab "):
                for token in entry[len("# branch.ab ") :].split():
                    if token.startswith("+"):
                        ahead = int(token[1:])
                    elif token.startswith("-"):
                        behind = int(token[1:])
            elif entry.startswith("#"):
                continue
            else:
                # "1 XY ... path" / "2 ... path" / "? path" / "u ..."
                changed.append(entry.rsplit(" ", 1)[-1])

        return GitStatus(
            branch=branch,
            head=head,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            clean=not changed,
            changed_files=tuple(sorted(changed)),
        )

    def diff(
        self,
        *,
        staged: bool = False,
        base: str | None = None,
        max_chars: int = DEFAULT_DIFF_LIMIT,
    ) -> GitDiff:
        args = ["diff", "--no-color", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        if base:
            args.append(base)
        patch = self.run(args)

        name_args = ["diff", "--name-only", "--no-color"]
        if staged:
            name_args.append("--cached")
        if base:
            name_args.append(base)
        files = tuple(line for line in self.run(name_args).splitlines() if line)

        truncated = len(patch) > max_chars
        return GitDiff(
            patch=patch[:max_chars],
            files=files,
            truncated=truncated,
            staged=staged,
        )

    def branch_exists(self, name: str) -> bool:
        result = self.run(["branch", "--list", "--format=%(refname:short)", name])
        return bool(result.strip())

    def preflight(self, task_id: str) -> Preflight:
        status = self.status()
        reasons: list[str] = []

        if status.detached:
            reasons.append("HEAD is detached; check out a branch first")
        if not status.clean:
            reasons.append("working tree has uncommitted changes")
        if not (self.path / ".git").exists():
            reasons.append("path is not a git repository")

        suggested = f"{BRANCH_PREFIX}/{task_id}"
        if self.branch_exists(suggested):
            reasons.append(f"task branch {suggested} already exists")

        return Preflight(
            ok=not reasons,
            reasons=tuple(reasons),
            status=status,
            suggested_branch=suggested,
        )


def ensure_git_repository(path: Path) -> None:
    """Cheap guard used before constructing a GitRepo for a stored allowlist entry."""
    if not path.is_dir() or not (path / ".git").exists():
        raise NotAGitRepository(
            "allowlisted path is no longer a git repository",
            details={"path": str(path)},
        )
