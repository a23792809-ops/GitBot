"""Recurring daily schedules API routes. Every endpoint requires the API token."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from gitbot.api.deps import require_token
from gitbot.api.schemas import ScheduleCreate, ScheduleUpdate
from gitbot.api.state import StateDep
from gitbot.errors import NotFoundError, PolicyViolation
from gitbot.models import AuditOutcome
from gitbot.tasks.schedules import detect_local_timezone_name

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

AUTH_DEPENDENCIES = [Depends(require_token)]


@router.get("", dependencies=AUTH_DEPENDENCIES)
def list_schedules(
    state: StateDep,
    repo_id: int | None = Query(default=None),
    enabled: bool | None = Query(default=None),
) -> dict[str, Any]:
    schedules = state.scheduler.schedules.list(repo_id=repo_id, enabled=enabled)
    return {
        "count": len(schedules),
        "schedules": [schedule.to_dict() for schedule in schedules],
        "detected_timezone": detect_local_timezone_name(),
    }


@router.post("", status_code=201, dependencies=AUTH_DEPENDENCIES)
def create_schedule(payload: ScheduleCreate, state: StateDep) -> dict[str, Any]:
    repo = state.repos.get(payload.repo_id)
    if repo is None:
        raise NotFoundError("repository is not allowlisted", details={"repo_id": payload.repo_id})
    if not repo.allows_task_type(payload.task_type):
        raise PolicyViolation(
            f"task type {payload.task_type.value!r} is disabled for this repository",
            details={"repo_id": payload.repo_id, "allowed": list(repo.allowed_task_types)},
        )

    schedule = state.scheduler.schedules.create(
        repo_id=payload.repo_id,
        task_type=payload.task_type,
        prompt=payload.prompt,
        local_time=payload.local_time,
        timezone=payload.timezone,
        enabled=payload.enabled,
    )
    state.audit.record(
        event_type="schedule",
        action="create",
        actor="user",
        target=schedule.id,
        outcome=AuditOutcome.SUCCESS,
        detail={
            "repo_id": schedule.repo_id,
            "task_type": schedule.task_type.value,
            "local_time": schedule.local_time,
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
        },
    )
    return {"schedule": schedule.to_dict()}


@router.get("/{schedule_id}", dependencies=AUTH_DEPENDENCIES)
def get_schedule(schedule_id: str, state: StateDep) -> dict[str, Any]:
    schedule = state.scheduler.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError("schedule not found", details={"id": schedule_id})
    return {"schedule": schedule.to_dict()}


@router.patch("/{schedule_id}", dependencies=AUTH_DEPENDENCIES)
def update_schedule(schedule_id: str, payload: ScheduleUpdate, state: StateDep) -> dict[str, Any]:
    schedule = state.scheduler.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError("schedule not found", details={"id": schedule_id})

    if payload.task_type is not None:
        repo = state.repos.get(schedule.repo_id)
        if repo is None:
            raise NotFoundError(
                "repository is not allowlisted", details={"repo_id": schedule.repo_id}
            )
        if not repo.allows_task_type(payload.task_type):
            raise PolicyViolation(
                f"task type {payload.task_type.value!r} is disabled for this repository",
                details={"repo_id": schedule.repo_id, "allowed": list(repo.allowed_task_types)},
            )

    updated = state.scheduler.schedules.update(
        schedule_id,
        task_type=payload.task_type,
        prompt=payload.prompt,
        local_time=payload.local_time,
        timezone=payload.timezone,
        enabled=payload.enabled,
    )
    state.audit.record(
        event_type="schedule",
        action="update",
        actor="user",
        target=schedule_id,
        outcome=AuditOutcome.SUCCESS,
        detail=payload.model_dump(exclude_unset=True),
    )
    return {"schedule": updated.to_dict()}


@router.post("/{schedule_id}/pause", dependencies=AUTH_DEPENDENCIES)
def pause_schedule(schedule_id: str, state: StateDep) -> dict[str, Any]:
    schedule = state.scheduler.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError("schedule not found", details={"id": schedule_id})

    updated = state.scheduler.schedules.set_enabled(schedule_id, False)
    state.audit.record(
        event_type="schedule",
        action="pause",
        actor="user",
        target=schedule_id,
        outcome=AuditOutcome.SUCCESS,
        detail={"enabled": False},
    )
    return {"schedule": updated.to_dict()}


@router.post("/{schedule_id}/resume", dependencies=AUTH_DEPENDENCIES)
def resume_schedule(schedule_id: str, state: StateDep) -> dict[str, Any]:
    schedule = state.scheduler.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError("schedule not found", details={"id": schedule_id})

    updated = state.scheduler.schedules.set_enabled(schedule_id, True)
    state.audit.record(
        event_type="schedule",
        action="resume",
        actor="user",
        target=schedule_id,
        outcome=AuditOutcome.SUCCESS,
        detail={"enabled": True},
    )
    return {"schedule": updated.to_dict()}


@router.delete("/{schedule_id}", dependencies=AUTH_DEPENDENCIES)
def delete_schedule(schedule_id: str, state: StateDep) -> dict[str, Any]:
    schedule = state.scheduler.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError("schedule not found", details={"id": schedule_id})

    deleted = state.scheduler.schedules.delete(schedule_id)
    state.audit.record(
        event_type="schedule",
        action="delete",
        actor="user",
        target=schedule_id,
        outcome=AuditOutcome.SUCCESS,
        detail={"repo_id": schedule.repo_id},
    )
    return {"deleted": deleted, "id": schedule_id}


@router.get("/{schedule_id}/history", dependencies=AUTH_DEPENDENCIES)
def schedule_history(
    schedule_id: str,
    state: StateDep,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    schedule = state.scheduler.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError("schedule not found", details={"id": schedule_id})

    entries = state.scheduler.schedules.history(schedule_id, limit=limit)
    return {
        "schedule_id": schedule_id,
        "count": len(entries),
        "history": entries,
    }
