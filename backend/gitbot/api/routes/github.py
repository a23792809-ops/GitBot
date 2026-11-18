"""Read-only GitHub endpoints backed by the local `gh` CLI.

All three endpoints expose account identity, accessible repositories, or
contribution activity, so they require the API token like every other read that
returns user data. The bind address remains loopback-only.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from gitbot.api.deps import require_token
from gitbot.api.state import StateDep

router = APIRouter(
    prefix="/api/github",
    tags=["github"],
    dependencies=[Depends(require_token)],
)
MAX_CONTRIBUTION_DAYS = 366


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


@router.get("/contributions")
def github_contributions(
    state: StateDep,
    from_date: Annotated[str | None, Query(alias="from")] = None,
    to_date: Annotated[str | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    """Read-only contribution activity; never writes to GitHub."""
    end = date.today() if to_date is None else _parse_date("to", to_date)
    start = end - timedelta(days=29) if from_date is None else _parse_date("from", from_date)
    if start > end:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if (end - start).days > MAX_CONTRIBUTION_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"date range must be at most {MAX_CONTRIBUTION_DAYS} days",
        )
    summary = state.github.contributions(from_date=start.isoformat(), to_date=end.isoformat())
    return {"contributions": summary.to_dict()}


def _parse_date(field: str, raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} must be an ISO date (YYYY-MM-DD)"
        ) from exc
