"""Tasks API: create, view status, run, cancel, approvals, and local commit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from gitbot.api.deps import require_token
from gitbot.api.state import StateDep
from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.gitops.repo import GitRepo, patch_touches_unsafe_git_metadata
from gitbot.models import ApprovalAction, TaskState, TaskType
from gitbot.opencode.adapter import OpenCodeAdapter
from gitbot.opencode.gateway import GatewayConfig
from gitbot.security.allowlist import default_allowed_roots, resolve_repo_path
from gitbot.tasks.approvals import ApprovalStore
from gitbot.tasks.engine import TaskEngine
from gitbot.timeutil import parse_iso_utc

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Everything except the (potentially large) AI response and stored patch. The
# individual task endpoint returns both in full.
_LIST_COLUMNS = (
    "id, repo_id, task_type, state, model, session_id, branch_name, diff_hash, "
    "approval_id, error_message, created_at, updated_at, started_at, finished_at, "
    "output_truncated, diff_truncated, scheduled_at, retry_count, max_retries, "
    "parent_task_id, tests_status, test_exit_code"
)
_LIST_PROMPT_CHARS = 200
_MAX_PROMPT_CHARS = 32_000
_MAX_MODEL_CHARS = 200


def _get_engine(state: StateDep) -> TaskEngine:
    db: Database = state.database
    audit: AuditLog = state.audit
    return TaskEngine(
        db,
        audit,
        opencode=OpenCodeAdapter(),
        dry_run=state.settings.dry_run,
        supervised_automation_enabled=state.settings.supervised_automation_enabled,
        allow_approved_local_commits=state.settings.allow_approved_local_commits,
        allowed_roots=default_allowed_roots(state.settings.home),
        retry_delay_s=state.settings.retry_delay_s,
        gateway=GatewayConfig(
            enabled=state.settings.ai_gateway_enabled,
            provider=state.settings.ai_gateway_provider,
            model=state.settings.ai_gateway_model,
            timeout_s=state.settings.ai_gateway_timeout_s,
            relay_port=state.settings.ai_gateway_relay_port,
        ),
    )


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
    if type(repo_id) is not int or repo_id < 1:
        raise HTTPException(status_code=422, detail="repo_id required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=422, detail="prompt required")
    prompt = prompt.strip()
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"prompt must be at most {_MAX_PROMPT_CHARS} characters",
        )
    if model is not None:
        if not isinstance(model, str):
            raise HTTPException(status_code=422, detail="model must be a string")
        model = model.strip()
        if not model or len(model) > _MAX_MODEL_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"model must be 1-{_MAX_MODEL_CHARS} characters",
            )
    try:
        ttype = TaskType(task_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid task_type") from exc
    if state.repos.get(repo_id) is None:
        raise HTTPException(status_code=422, detail="repo_id is not allowlisted")

    scheduled_at = payload.get("scheduled_at")
    if scheduled_at is not None and not isinstance(scheduled_at, str):
        raise HTTPException(status_code=422, detail="scheduled_at must be an ISO-8601 string")
    max_retries = payload.get("max_retries", 0)
    if type(max_retries) is not int or not 0 <= max_retries <= 5:
        raise HTTPException(
            status_code=422,
            detail="max_retries must be an integer between 0 and 5",
        )

    task_id = engine.create_task(
        repo_id,
        ttype,
        prompt,
        model=model,
        scheduled_at=scheduled_at,
        max_retries=max_retries,
    )
    return {"task": engine.get_task(task_id)}


@router.get("", dependencies=[Depends(require_token)])
def list_tasks(state: StateDep) -> dict[str, Any]:
    with state.database.connect() as conn:
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS}, substr(prompt, 1, {_LIST_PROMPT_CHARS}) AS prompt "  # noqa: S608 - fixed columns
            "FROM tasks ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    tasks = [dict(r) for r in rows]
    return {"tasks": tasks, "count": len(tasks)}


@router.get("/scheduler/status", dependencies=[Depends(require_token)])
def scheduler_status(state: StateDep) -> dict[str, Any]:
    return {"scheduler": state.scheduler.status()}


@router.post("/scheduler/tick", dependencies=[Depends(require_token)])
def scheduler_tick(state: StateDep) -> dict[str, Any]:
    """Run any due scheduled tasks now, regardless of the background toggle."""
    result = state.scheduler.tick()
    return {"scheduler": result}


@router.get("/{task_id}", dependencies=[Depends(require_token)])
def get_task(state: StateDep, task_id: str) -> dict[str, Any]:
    with state.database.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()
    task_dict = dict(task)
    return {
        "task": task_dict,
        "events": [dict(e) for e in events],
        "diff": _stored_diff(task_dict),
    }


def _stored_diff(task: dict[str, Any]) -> dict[str, Any] | None:
    """Return the diff as reviewed, without touching the repository."""
    patch = task.get("diff_patch")
    if patch is None:
        return None
    try:
        files = json.loads(task.get("diff_files") or "[]")
    except json.JSONDecodeError:
        files = []
    return {
        "patch": patch,
        "files": files if isinstance(files, list) else [],
        "truncated": bool(task.get("diff_truncated")),
        "staged": False,
        "stored": True,
    }


@router.post("/{task_id}/run", dependencies=[Depends(require_token)])
def run_task(
    task_id: str,
    engine: TaskEngine = Depends(_get_engine),  # noqa: B008
) -> dict[str, Any]:
    return {"task": engine.run_task(task_id)}


@router.post("/{task_id}/cancel", dependencies=[Depends(require_token)])
def cancel_task(
    task_id: str,
    engine: TaskEngine = Depends(_get_engine),  # noqa: B008
) -> dict[str, Any]:
    return {"task": engine.cancel_task(task_id)}


@router.post("/{task_id}/retry", dependencies=[Depends(require_token)])
def retry_task(
    task_id: str,
    engine: TaskEngine = Depends(_get_engine),  # noqa: B008
) -> dict[str, Any]:
    return {"task": engine.retry_task(task_id)}


@router.post("/{task_id}/commit", dependencies=[Depends(require_token)])
def commit_task(
    task_id: str,
    engine: TaskEngine = Depends(_get_engine),  # noqa: B008
) -> dict[str, Any]:
    return {"task": engine.commit_task(task_id)}


@router.get("/approvals/pending", dependencies=[Depends(require_token)])
def pending_approvals(state: StateDep) -> dict[str, Any]:
    """Only approvals that can still be acted on: unused, unexpired, in review."""
    with state.database.connect() as conn:
        rows = conn.execute(
            "SELECT a.* FROM approvals a JOIN tasks t ON t.id = a.task_id "
            "WHERE a.used_at IS NULL AND t.state = ? "
            "AND a.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "ORDER BY a.created_at DESC",
            (TaskState.AWAITING_REVIEW.value,),
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
    approval = store.get(approval_id)
    if approval.used_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "approval_invalid", "message": "approval already used"}},
        )
    if approval.action is not ApprovalAction.COMMIT:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "approval_invalid",
                    "message": "approval is not a commit approval",
                }
            },
        )
    try:
        expires_at = parse_iso_utc(approval.expires_at)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "approval_invalid", "message": "invalid approval expiry"}},
        ) from exc
    if datetime.now(UTC) >= expires_at:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "approval_expired", "message": "approval expired"}},
        )

    # Only an unreviewed task can be approved; check before consuming the token.
    with state.database.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (approval.task_id,)).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["approval_id"] != approval_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "approval_invalid",
                    "message": "approval is not bound to this task",
                }
            },
        )
    if task["state"] != TaskState.AWAITING_REVIEW.value:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "invalid_state_transition",
                    "message": f"cannot approve a task in state {task['state']}",
                }
            },
        )

    # A patch that writes Git metadata or selects a filter driver can never be
    # committed; refuse to bind an approval to it instead of letting the user
    # approve something uncommittable.
    unsafe_path = patch_touches_unsafe_git_metadata(task["diff_patch"] or "")
    if unsafe_path is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "policy_violation",
                    "message": (
                        f"the reviewed patch writes Git metadata or a Git filter attribute "
                        f"({unsafe_path}); re-run the task with a smaller change"
                    ),
                }
            },
        )

    # An approval must describe exactly what was reviewed. All approvals for
    # sandboxed tasks bind to the stored patch and to the repository state at
    # review time; legacy rows without sandbox metadata are refused below.
    repo = state.repos.get(approval.repo_id)
    if repo is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "approval references a repository that is no longer allowlisted",
                }
            },
        )
    if repo.id != task["repo_id"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "approval references a different repository than the task",
                }
            },
        )
    resolved = resolve_repo_path(
        repo.path, allowed_roots=default_allowed_roots(state.settings.home)
    )
    if not task["sandboxed"] or not task["snapshot_head"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "approval_invalid",
                    "message": (
                        "legacy task has no sandboxed review diff; re-run it before approving"
                    ),
                }
            },
        )
    reviewed_hash = task["diff_hash"]
    status = GitRepo(resolved).status()
    if not status.clean:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "the working tree changed since this approval was created",
                }
            },
        )
    if task["branch_name"] and status.branch != task["branch_name"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "the repository is no longer on the reviewed branch",
                }
            },
        )
    if status.head != task["snapshot_head"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "the repository HEAD changed since this approval was created",
                }
            },
        )
    if not reviewed_hash or diff_hash != reviewed_hash:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "the submitted hash does not match the reviewed diff",
                }
            },
        )
    if approval.diff_hash != reviewed_hash:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "approval_mismatch",
                    "message": "the approval does not match the stored review diff",
                }
            },
        )

    tests_status = task["tests_status"] or "not_run"
    if tests_status in ("failed", "error"):
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "invalid_state_transition",
                    "message": (
                        "cannot approve a task whose sandboxed tests did not pass; "
                        "fix the change and re-run the task"
                    ),
                }
            },
        )

    # The approval is single-use, but it is consumed at commit time, not here:
    # approving is reversible, committing is not. The state update is guarded so
    # two concurrent approvals cannot both succeed.
    with state.database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "approval_id = ? WHERE id = ? AND state = ?",
            (
                TaskState.APPROVED.value,
                approval_id,
                approval.task_id,
                TaskState.AWAITING_REVIEW.value,
            ),
        )
        if cursor.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "invalid_state_transition",
                        "message": "task left review before the approval was applied",
                    }
                },
            )
        conn.execute(
            "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
            "VALUES (?, ?, 'task.approved', 'user', ?)",
            (
                approval.task_id,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                json.dumps({"approval_id": approval_id, "diff_hash": reviewed_hash}),
            ),
        )
    return {"approved": True, "approval_id": approval_id}


@router.post("/approvals/{approval_id}/reject", dependencies=[Depends(require_token)])
def reject_request(state: StateDep, approval_id: str) -> dict[str, Any]:
    store = ApprovalStore(state.database)
    approval = store.get(approval_id)
    if approval.used_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "approval_invalid", "message": "approval already used"}},
        )
    with state.database.transaction() as conn:
        task = conn.execute("SELECT state FROM tasks WHERE id = ?", (approval.task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task["state"] not in (
            TaskState.AWAITING_REVIEW.value,
            TaskState.QUEUED.value,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "invalid_state_transition",
                        "message": f"cannot reject a task in state {task['state']}",
                    }
                },
            )
        cursor = conn.execute(
            "UPDATE tasks SET state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "finished_at = COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "WHERE id = ? AND state IN ('awaiting_review','queued')",
            (TaskState.CANCELLED.value, approval.task_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "invalid_state_transition",
                        "message": "task left review before the rejection was applied",
                    }
                },
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
        conn.execute(
            "UPDATE approvals SET used_at = COALESCE("
            "used_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "WHERE id = ?",
            (approval_id,),
        )
    return {"rejected": True, "approval_id": approval_id}
