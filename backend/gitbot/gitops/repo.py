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

from gitbot.errors import GitBotError, PolicyViolation, UpstreamError, UpstreamTimeout
from gitbot.security import redact_text
from gitbot.security.allowlist import assert_no_filter_drivers

DEFAULT_DIFF_LIMIT = 256 * 1024  # characters of patch text
BRANCH_PREFIX = "gitbot"


class NotAGitRepository(GitBotError):
    code = "not_a_git_repository"
    http_status = 400


class GitCommandError(UpstreamError):
    code = "git_command_error"


def validate_diff_base(base: str) -> str:
    """Validate a user-supplied diff base before it becomes a git argument.

    Git options are accepted anywhere before a ``--`` path separator, so a value
    such as ``--output=/home/user/.bashrc`` would be a write primitive when
    passed through blindly.  Revisions cannot contain whitespace or start with
    ``-``; rejecting those shapes keeps the value on git's revision side of the
    command line.  The caller also inserts ``--`` after the revision.
    """
    value = base.strip()
    if not value or value.startswith("-"):
        raise PolicyViolation("invalid base revision", details={"base": base[:200]})
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise PolicyViolation("invalid base revision", details={"base": base[:200]})
    return value


def patch_touches_unsafe_git_metadata(patch: str) -> str | None:
    """Return a path when a patch could install Git metadata or a filter.

    Applying a patch must not be able to write ``.git`` internals or add,
    modify, or rename a ``.gitattributes`` file.  Git attributes can select a
    filter driver, which would execute a repository-controlled command during a
    later Git operation.  Rejecting every attribute update is intentionally
    conservative; GitBot does not support filter drivers.
    """
    for line in patch.splitlines():
        path: str | None = None
        if line.startswith("diff --git "):
            header = line[len("diff --git ") :]
            if ".gitattributes" in header:
                return ".gitattributes"
            if " b/.git/" in header:
                return header.split(" b/", 1)[1].strip() or ".git"
            if header.endswith(" b/.git"):
                return ".git"
        elif (
            line.startswith("rename from ")
            or line.startswith("copy from ")
            or line.startswith("rename to ")
            or line.startswith("copy to ")
        ):
            path = line.split(" ", 2)[2].strip().strip('"')
        elif line.startswith("--- a/"):
            path = line[len("--- a/") :].strip().strip('"')
        elif line.startswith("+++ b/"):
            path = line[len("+++ b/") :].strip().strip('"')
        if path is None:
            continue
        if path == ".git" or path.startswith(".git/"):
            return path
        if path == ".gitattributes" or path.endswith("/.gitattributes"):
            return path
    return None


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


# Command-line configuration is more specific than repository or global
# configuration, so these values cannot be overridden by a malicious local
# ``.git/config``.  Hooks and fsmonitor commands are repository-controlled code
# and must never execute when GitBot inspects or commits an allowlisted repo.
_SAFE_GIT_CONFIG: tuple[str, ...] = (
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

# Variables that either select a different repository/index or inject commands
# into git.  The host environment is untrusted for this purpose: GitBot must
# operate on ``self.path`` only.
_SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_CONFIG_HOME"})


def _git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    environment = {key: source[key] for key in _SAFE_ENV_KEYS if key in source}
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = "/bin/false"
    environment["GIT_PAGER"] = "cat"
    environment["GIT_EDITOR"] = ":"
    environment["GIT_SEQUENCE_EDITOR"] = ":"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
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
        if (self.path / ".git").exists():
            assert_no_filter_drivers(self.path)

    # -- plumbing --------------------------------------------------------
    def run(self, args: Sequence[str], *, timeout_s: float | None = None) -> str:
        argv = [self.executable, *_SAFE_GIT_CONFIG, *args]
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
        include_untracked: bool = False,
    ) -> GitDiff:
        if include_untracked and not staged and not base:
            # Refusing loudly instead of silently mutating the index: a
            # read-only inspection module must never run `git add`. Callers
            # that need untracked files must snapshot them explicitly.
            raise PolicyViolation("include_untracked is not supported; it would mutate the index")
        safe_base = validate_diff_base(base) if base is not None else None
        args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        if safe_base:
            args.append(safe_base)
        # A terminator prevents any path/revision from being interpreted as an
        # option (notably --output=<file>, which writes outside the repository).
        args.append("--")
        patch = self.run(args)

        name_args = ["diff", "--name-only", "--no-color"]
        if staged:
            name_args.append("--cached")
        if safe_base:
            name_args.append(safe_base)
        name_args.append("--")
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
