"""Read-only GitHub endpoints backed by the local `gh` CLI."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from gitbot.api.state import StateDep

router = APIRouter(prefix="/api/github", tags=["github"])


@router.get("/status")
def github_status(state: StateDep) -> dict[str, Any]:
    return state.github.status().to_dict()


@router.get("/repos")
def github_repos(
    state: StateDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    refresh: bool = False,
) -> dict[str, Any]:
    repos = state.github.repos(limit=limit, refresh=refresh)
    return {
        "count": len(repos),
        "repos": [repo.to_dict() for repo in repos],
        "read_only": True,
    }
