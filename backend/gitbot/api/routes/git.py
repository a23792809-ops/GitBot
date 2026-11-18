"""Read-only git endpoints for allowlisted repositories."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from gitbot.api.deps import require_token
from gitbot.api.state import StateDep
from gitbot.errors import GitBotError, NotFoundError, PolicyViolation
from gitbot.gitops.repo import GitRepo, validate_diff_base
from gitbot.models import Repo
from gitbot.security.allowlist import default_allowed_roots, resolve_repo_path

# Status/diff/preflight all reveal repository paths or file contents, so the
# whole router is token-gated.  The bind address remains loopback-only.
router = APIRouter(
    prefix="/api/repos/{repo_id}/git",
    tags=["git"],
    dependencies=[Depends(require_token)],
)

MAX_PATCH_CHARS = 512 * 1024


def _allowlisted_repo(repo_id: int, state: StateDep) -> tuple[Repo, Path]:
    """Re-validate the stored path on every use; fail closed if it no longer holds."""
    repo = state.repos.get(repo_id)
    if repo is None:
        raise NotFoundError("repository is not allowlisted", details={"id": repo_id})
    try:
        resolved = resolve_repo_path(
            repo.path, allowed_roots=default_allowed_roots(state.settings.home)
        )
    except GitBotError as exc:
        raise NotFoundError(
            "allowlisted repository path no longer exists or is no longer safe to use",
            details={"id": repo_id, "path": repo.path, "reason": exc.message},
        ) from exc
    return repo, resolved


@router.get("/status")
def git_status(repo_id: int, state: StateDep) -> dict[str, Any]:
    repo, resolved = _allowlisted_repo(repo_id, state)
    status = GitRepo(resolved).status()
    return {"repo": repo.slug, "path": str(resolved), "status": status.to_dict()}


@router.get("/diff")
def git_diff(
    repo_id: int,
    state: StateDep,
    staged: bool = False,
    base: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    repo, resolved = _allowlisted_repo(repo_id, state)
    try:
        safe_base = validate_diff_base(base) if base is not None else None
    except PolicyViolation as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    diff = GitRepo(resolved).diff(staged=staged, base=safe_base, max_chars=MAX_PATCH_CHARS)
    return {"repo": repo.slug, "diff": diff.to_dict()}


@router.get("/preflight")
def git_preflight(
    repo_id: int,
    state: StateDep,
    task_id: Annotated[str, Query(min_length=1, max_length=64)] = "check",
) -> dict[str, Any]:
    repo, resolved = _allowlisted_repo(repo_id, state)
    preflight = GitRepo(resolved).preflight(task_id)
    return {"repo": repo.slug, "preflight": preflight.to_dict()}
