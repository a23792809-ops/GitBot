"""Path validation for the repository allowlist.

Defence in depth: a path must exist, be a directory, be a git repository, live
inside an allowed root, and never point inside GitBot itself. Symlinks are
resolved *before* the containment check, so ``repo/link -> /etc`` cannot escape.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from gitbot.errors import PolicyViolation

# backend/gitbot/security/allowlist.py -> repository root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AllowlistError(PolicyViolation):
    code = "path_not_allowed"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def default_allowed_roots(home: Path) -> tuple[Path, ...]:
    return (home.resolve(),)


_SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_CONFIG_HOME"})


def _safe_git_env() -> dict[str, str]:
    source = os.environ
    environment = {key: source[key] for key in _SAFE_ENV_KEYS if key in source}
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = "/bin/false"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    return environment


def _git_directory(repo: Path) -> Path | None:
    git_path = repo / ".git"
    if git_path.is_dir() and not git_path.is_symlink():
        return git_path
    if not git_path.is_file():
        return None
    try:
        text = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.lower().startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = repo / target
    try:
        return target.resolve(strict=True)
    except OSError:
        return None


def _attributes_select_filter(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AllowlistError(
            "repository attributes could not be read",
            details={"path": str(path)},
        ) from exc
    for line in text.splitlines():
        for token in line.split("#", 1)[0].split():
            if token.startswith("filter=") and len(token) > len("filter="):
                return True
    return False


def assert_no_filter_drivers(repo: Path) -> None:
    """Fail closed when repository attributes select a Git filter driver.

    Git expands ``filter=<name>`` attributes by running commands configured in
    ``.git/config``/global config.  That would execute code supplied by an
    allowlisted repository during ``git status``, ``git diff``, or ``git
    apply``.  GitBot does not support filter drivers; refuse the repository
    instead of weakening the security boundary.

    ``git check-attr`` only reads attribute files.  It does not execute the
    filter commands it reports.
    """
    git = shutil.which("git")
    if git is None:
        raise AllowlistError(
            "the git executable is required to inspect repository attributes",
            details={"path": str(repo)},
        )
    paths = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            git,
            "-C",
            str(repo),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        timeout=30,
        check=False,
        env=_safe_git_env(),
    )
    if paths.returncode != 0:
        raise AllowlistError(
            "repository files could not be inspected for Git filter attributes",
            details={"path": str(repo)},
        )
    checked = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            git,
            "-C",
            str(repo),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "check-attr",
            "-z",
            "--stdin",
            "filter",
        ],
        input=paths.stdout,
        capture_output=True,
        timeout=30,
        check=False,
        env=_safe_git_env(),
    )
    if checked.returncode != 0:
        raise AllowlistError(
            "repository attributes could not be inspected",
            details={"path": str(repo)},
        )
    fields = checked.stdout.split(b"\0")
    for index in range(0, len(fields) - 2, 3):
        path, attribute, value = fields[index : index + 3]
        if attribute != b"filter":
            continue
        if value in (b"", b"unset", b"unspecified"):
            continue
        raise AllowlistError(
            "repository uses a Git filter driver; GitBot refuses to execute "
            "repository-controlled filter commands",
            details={"path": os.fsdecode(path), "filter": os.fsdecode(value)},
        )

    # ``check-attr`` only reports attributes for paths that exist.  A filter
    # rule can still be waiting in an attribute file with no matching file yet,
    # so inspect the attribute files themselves and ``info/attributes``.
    attribute_files = [
        repo / os.fsdecode(item)
        for item in paths.stdout.split(b"\0")
        if item and os.fsdecode(item).endswith(".gitattributes")
    ]
    git_dir = _git_directory(repo)
    if git_dir is not None:
        attribute_files.append(git_dir / "info" / "attributes")
    for attribute_file in attribute_files:
        if attribute_file.is_file() and _attributes_select_filter(attribute_file):
            raise AllowlistError(
                "repository uses a Git filter driver; GitBot refuses to execute "
                "repository-controlled filter commands",
                details={"path": str(attribute_file)},
            )


def _validate_git_metadata(
    repo: Path,
    *,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path],
) -> None:
    """Reject git metadata that redirects outside the reviewed repository.

    A normal clone stores ``.git`` as a directory inside the repository.  A
    worktree/submodule uses a ``.git`` file whose ``gitdir:`` target is another
    path.  GitBot accepts that only when the target exists and remains inside
    the user's allowed roots; a symlinked ``.git`` or an arbitrary target would
    let git inspect or commit to a repository the user did not allowlist.
    """
    git_path = repo / ".git"
    if git_path.is_symlink():
        raise AllowlistError(
            "repository .git metadata must not be a symlink",
            details={"path": str(git_path)},
        )
    if git_path.is_dir():
        return
    if not git_path.is_file():
        raise AllowlistError(
            "path is not a git repository",
            details={"path": str(repo)},
        )
    try:
        text = git_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AllowlistError(
            "repository .git file cannot be read",
            details={"path": str(git_path)},
        ) from exc
    if not text.lower().startswith("gitdir:"):
        raise AllowlistError(
            "repository .git file does not contain a gitdir target",
            details={"path": str(git_path)},
        )
    raw_target = text.split(":", 1)[1].strip()
    target = Path(raw_target)
    if not target.is_absolute():
        target = repo / target
    try:
        target = target.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise AllowlistError(
            "repository .git metadata target does not exist",
            details={"path": str(git_path), "target": raw_target},
        ) from exc
    if not target.is_dir():
        raise AllowlistError(
            "repository .git metadata target is not a directory",
            details={"path": str(git_path), "target": str(target)},
        )
    if any(_is_within(target, forbidden) for forbidden in forbidden_roots):
        raise AllowlistError(
            "repository .git metadata points into GitBot's source tree",
            details={"path": str(git_path), "target": str(target)},
        )
    if not any(_is_within(target, root) for root in allowed_roots):
        raise AllowlistError(
            "repository .git metadata is outside the allowed roots",
            details={"path": str(git_path), "target": str(target)},
        )


def resolve_repo_path(
    raw: str | os.PathLike[str],
    *,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (PROJECT_ROOT,),
    require_git: bool = True,
) -> Path:
    """Validate a user-supplied path and return its canonical form."""
    text = os.fspath(raw).strip()
    if not text:
        raise AllowlistError("repository path is empty")

    expanded = Path(os.path.expanduser(text))
    if not expanded.is_absolute():
        raise AllowlistError(
            "repository path must be absolute",
            details={"path": text},
        )

    try:
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AllowlistError(
            "repository path does not exist",
            details={"path": text},
        ) from exc
    except OSError as exc:
        raise AllowlistError(
            "repository path cannot be resolved",
            details={"path": text},
        ) from exc

    if not resolved.is_dir():
        raise AllowlistError(
            "repository path is not a directory",
            details={"path": str(resolved)},
        )

    if require_git and not (resolved / ".git").exists():
        raise AllowlistError(
            "path is not a git repository",
            details={"path": str(resolved)},
        )

    for forbidden in forbidden_roots:
        forbidden_resolved = Path(forbidden).resolve()
        if resolved == forbidden_resolved or _is_within(resolved, forbidden_resolved):
            raise AllowlistError(
                "GitBot refuses to operate on its own source tree",
                details={"path": str(resolved)},
            )

    roots = [Path(root).resolve() for root in allowed_roots]
    if not any(_is_within(resolved, root) for root in roots):
        raise AllowlistError(
            "repository path is outside the allowed roots",
            details={"path": str(resolved), "allowed_roots": [str(root) for root in roots]},
        )

    if require_git:
        _validate_git_metadata(
            resolved,
            allowed_roots=roots,
            forbidden_roots=[Path(root).resolve() for root in forbidden_roots],
        )
        assert_no_filter_drivers(resolved)

    return resolved
