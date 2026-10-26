"""Read-only git endpoints for allowlisted repositories."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query

from gitbot.api.state import StateDep
from gitbot.errors import NotFoundError
from gitbot.gitops.repo import GitRepo, ensure_git_repository
from gitbot.models import Repo

router = APIRouter(prefix="/api/repos/{repo_id}/git", tags=["git"])

MAX_PATCH_CHARS = 512 * 1024


def _allowlisted_repo(repo_id: int, state: StateDep) -> Repo:
    repo = state.repos.get(repo_id)
    if repo is None:
        raise NotFoundError("repository is not allowlisted", details={"id": repo_id})
    path = Path(repo.path)
    if not path.is_dir():
        raise NotFoundError(
            "allowlisted repository path no longer exists",
            details={"id": repo_id, "path": repo.path},
        )
    ensure_git_repository(path)
    return repo


@router.get("/status")
def git_status(repo_id: int, state: StateDep) -> dict[str, Any]:
    repo = _allowlisted_repo(repo_id, state)
    status = GitRepo(repo.path).status()
    return {"repo": repo.slug, "path": repo.path, "status": status.to_dict()}


@router.get("/diff")
def git_diff(
    repo_id: int,
    state: StateDep,
    staged: bool = False,
    base: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    repo = _allowlisted_repo(repo_id, state)
    diff = GitRepo(repo.path).diff(staged=staged, base=base, max_chars=MAX_PATCH_CHARS)
    return {"repo": repo.slug, "diff": diff.to_dict()}


@router.get("/preflight")
def git_preflight(
    repo_id: int,
    state: StateDep,
    task_id: Annotated[str, Query(min_length=1, max_length=64)] = "check",
) -> dict[str, Any]:
    repo = _allowlisted_repo(repo_id, state)
    preflight = GitRepo(repo.path).preflight(task_id)
    return {"repo": repo.slug, "preflight": preflight.to_dict()}
