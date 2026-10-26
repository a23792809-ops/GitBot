"""Audit log reads. The log itself is append-only in SQLite."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from gitbot.api.state import StateDep
from gitbot.audit import MAX_LIMIT

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
def list_audit_entries(
    state: StateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    event_type: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> dict[str, Any]:
    entries = state.audit.list_entries(limit=limit, before_id=before_id, event_type=event_type)
    return {"count": len(entries), "entries": [entry.to_dict() for entry in entries]}
