"""Tasks API: create, view status, approvals."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from gitbot.api.deps import require_token
from gitbot.api.state import StateDep
from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import GitBotError
from gitbot.models import TaskState, TaskType
from gitbot.opencode.adapter import OpenCodeAdapter
from gitbot.tasks.approvals import ApprovalStore
from gitbot.tasks.engine import TaskEngine

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _get_engine(state: StateDep) -> TaskEngine:
    db: Database = state.database
    audit: AuditLog = state.audit
    return TaskEngine(db, audit, opencode=OpenCodeAdapter())


@router.post("", dependencies=[Depends(require_token)])
def create_task(
    state: StateDep,
    payload: dict[str, Any],
    engine: TaskEngine = Depends(_get_engine),  # noqa: B008
) -> dict[str, Any]:
    repo_id = payload.get("repo_id")
    task_type = payload.get("task_type", "analyze")
    prompt = payload.get("prompt", "")
    model = payload.get("model")
    if not isinstance(repo_id, int):
        raise HTTPException(status_code=422, detail="repo_id required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=422, detail="prompt required")
    try:
        ttype = TaskType(task_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid task_type") from exc
    try:
        task_id = engine.create_task(repo_id, ttype, prompt.strip(), model=model)
        task = engine.get_task(task_id)
        return {"task": task}
    except GitBotError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc


@router.get("")
def list_tasks(state: StateDep) -> dict[str, Any]:
    with state.database.connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100").fetchall()
    tasks = [dict(r) for r in rows]
    return {"tasks": tasks, "count": len(tasks)}


@router.get("/{task_id}")
def get_task(state: StateDep, task_id: str) -> dict[str, Any]:
    with state.database.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()
    return {"task": dict(task), "events": [dict(e) for e in events]}


@router.post("/{task_id}/run", dependencies=[Depends(require_token)])
def run_task(
    task_id: str,
    engine: TaskEngine = Depends(_get_engine),  # noqa: B008
) -> dict[str, Any]:
    try:
        task = engine.run_task(task_id)
        return {"task": task}
    except GitBotError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc


@router.get("/approvals/pending")
def pending_approvals(state: StateDep) -> dict[str, Any]:
    with state.database.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE used_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    approvals = [dict(r) for r in rows]
    return {"approvals": approvals, "count": len(approvals)}


@router.post("/approvals/{approval_id}/approve", dependencies=[Depends(require_token)])
def approve_request(
    state: StateDep,
    approval_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    diff_hash = payload.get("diff_hash")
    if not isinstance(diff_hash, str) or not diff_hash:
        raise HTTPException(status_code=422, detail="diff_hash required")
    store = ApprovalStore(state.database)
    try:
        approval = store.get(approval_id)
    except GitBotError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    try:
        consumed = store.consume(
            approval_id,
            task_id=approval.task_id,
            repo_id=approval.repo_id,
            diff_hash=diff_hash,
        )
    except GitBotError as exc:
        status = 403
        if exc.code in ("approval_expired",):
            status = 410
        raise HTTPException(status_code=status, detail=exc.to_dict()) from exc
    # Update task state if awaiting review/approved flow
    with state.database.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (consumed.task_id,)).fetchone()
        if task and task["state"] in (TaskState.AWAITING_REVIEW.value, TaskState.QUEUED.value):
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
                "approval_id = ? WHERE id = ?",
                (TaskState.APPROVED.value, approval_id, consumed.task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, 'task.approved', 'user', ?)",
                (
                    consumed.task_id,
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    json.dumps({"approval_id": approval_id}),
                ),
            )
    return {"approved": True, "approval_id": approval_id}


@router.post("/approvals/{approval_id}/reject", dependencies=[Depends(require_token)])
def reject_request(state: StateDep, approval_id: str) -> dict[str, Any]:
    store = ApprovalStore(state.database)
    try:
        approval = store.get(approval_id)
    except GitBotError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    if approval.used_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "approval_invalid", "message": "approval already used"}},
        )
    # Mark as used by consuming logic not required; just record rejection
    with state.database.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND state IN ('awaiting_review','queued')",
            (TaskState.CANCELLED.value, approval.task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
            "VALUES (?, ?, 'task.rejected', 'user', ?)",
            (
                approval.task_id,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                json.dumps({"approval_id": approval_id}),
            ),
        )
        # Mark approval used
        conn.execute(
            "UPDATE approvals SET used_at = COALESCE("
            "used_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "WHERE id = ?",
            (approval_id,),
        )
    return {"rejected": True, "approval_id": approval_id}
