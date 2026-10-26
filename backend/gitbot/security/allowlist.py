"""Path validation for the repository allowlist.

Defence in depth: a path must exist, be a directory, be a git repository, live
inside an allowed root, and never point inside GitBot itself. Symlinks are
resolved *before* the containment check, so ``repo/link -> /etc`` cannot escape.
"""

from __future__ import annotations

import os
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

    return resolved
