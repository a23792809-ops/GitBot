"""Allowlisted repositories (read-only in Phase 1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from gitbot.api.state import StateDep

router = APIRouter(prefix="/api/repos", tags=["repositories"])


@router.get("")
def list_repos(state: StateDep) -> dict[str, Any]:
    repos = state.repos.list()
    return {
        "count": len(repos),
        "repos": [repo.to_dict() for repo in repos],
        "note": "Only allowlisted repositories may be used for tasks.",
    }
