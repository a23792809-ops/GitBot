"""Opt-in scheduler for queued tasks with a due ``scheduled_at`` timestamp.

Scheduling is intentionally conservative:

* a task is only due when it is still ``queued`` and its stored UTC timestamp
  has passed;
* a process-local lock prevents two ticks from claiming the same batch;
* only one task runs at a time, matching the single-process execution model;
* change tasks are skipped while dry-run is enabled instead of failing forever;
* every attempt is recorded as a task event so a skipped or failing schedule is
  visible in the dashboard.

There is no cron expression parser and no arbitrary command execution: a
schedule only decides *when* an already-created, validated task may run.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import GitBotError
from gitbot.models import (
    READ_ONLY_TASK_TYPES,
    AuditOutcome,
    TaskState,
    TaskType,
)
from gitbot.opencode.adapter import OpenCodeAdapter
from gitbot.opencode.gateway import GatewayConfig
from gitbot.security import redact_value
from gitbot.security.allowlist import default_allowed_roots
from gitbot.tasks.engine import TaskEngine
from gitbot.tasks.schedules import (
    ScheduleStore,
    due_occurrence,
    next_run_at,
)
from gitbot.timeutil import format_utc

logger = logging.getLogger("gitbot.tasks.scheduler")

DEFAULT_INTERVAL_S = 30
DEFAULT_BATCH_SIZE = 5
MAX_BATCH_SIZE = 25


def _now() -> str:
    return format_utc(datetime.now(UTC))


class Scheduler:
    def __init__(
        self,
        database: Database,
        audit: AuditLog,
        *,
        dry_run: bool,
        supervised_automation_enabled: bool = False,
        allow_approved_local_commits: bool = False,
        allowed_roots: Sequence[Path] | None = None,
        home: Path | None = None,
        interval_s: int = DEFAULT_INTERVAL_S,
        batch_size: int = DEFAULT_BATCH_SIZE,
        engine_factory: Callable[[], TaskEngine] | None = None,
        gateway: GatewayConfig | None = None,
        retry_delay_s: int = 30,
    ) -> None:
        self.db = database
        self.audit = audit
        self.dry_run = dry_run
        self.supervised_automation_enabled = bool(supervised_automation_enabled)
        self.allow_approved_local_commits = bool(allow_approved_local_commits)
        self.interval_s = max(1, int(interval_s))
        self.batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
        self.schedules = ScheduleStore(database)
        roots = tuple(allowed_roots) if allowed_roots is not None else None
        self._home = home or Path.home()
        self._engine_factory = engine_factory or (
            lambda: TaskEngine(
                database,
                audit,
                opencode=OpenCodeAdapter(),
                dry_run=dry_run,
                supervised_automation_enabled=self.supervised_automation_enabled,
                allow_approved_local_commits=self.allow_approved_local_commits,
                allowed_roots=roots if roots is not None else default_allowed_roots(self._home),
                gateway=gateway,
                retry_delay_s=retry_delay_s,
            )
        )
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- queries ---------------------------------------------------------
    def due_task_ids(self, *, now: str | None = None, limit: int | None = None) -> list[str]:
        effective_now = now or _now()
        bounded = max(1, min(limit or self.batch_size, MAX_BATCH_SIZE))
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE state = ? AND scheduled_at IS NOT NULL "
                "AND scheduled_at <= ? ORDER BY scheduled_at ASC, created_at ASC LIMIT ?",
                (TaskState.QUEUED.value, effective_now, bounded),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def next_scheduled_at(self) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT MIN(scheduled_at) AS next_at FROM tasks "
                "WHERE state = ? AND scheduled_at IS NOT NULL",
                (TaskState.QUEUED.value,),
            ).fetchone()
        value = row["next_at"] if row is not None else None
        return str(value) if value else None

    def next_schedule_at(self, *, now: datetime | None = None) -> str | None:
        """Earliest next recurring-schedule occurrence, if any is enabled."""
        current = now or datetime.now(UTC)
        values: list[str] = []
        for schedule in self.schedules.list(enabled=True):
            try:
                value = next_run_at(schedule, current)
            except Exception:  # a corrupt timezone must not break health
                logger.exception("could not compute next run for schedule %s", schedule.id)
                continue
            if value:
                values.append(value)
        return min(values) if values else None

    def status(self) -> dict[str, object]:
        return {
            "enabled": self._thread is not None and self._thread.is_alive(),
            "interval_seconds": self.interval_s,
            "batch_size": self.batch_size,
            "dry_run": self.dry_run,
            "supervised_automation_enabled": self.supervised_automation_enabled,
            "allow_approved_local_commits": self.allow_approved_local_commits,
            "next_scheduled_at": self.next_scheduled_at(),
            "next_schedule_at": self.next_schedule_at(),
            "schedule_count": self.schedules.count(),
            "enabled_schedule_count": len(self.schedules.list(enabled=True)),
        }

    # -- execution -------------------------------------------------------
    def trigger_due_schedules(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        """Inspect enabled recurring schedules, claim any due date atomically, and create tasks."""
        current_now = now or datetime.now(UTC)
        triggered: list[dict[str, object]] = []
        for schedule in self.schedules.list(enabled=True):
            try:
                due = due_occurrence(schedule, current_now)
            except Exception:
                logger.exception("could not compute due occurrence for schedule %s", schedule.id)
                continue
            if due is None:
                continue

            local_date = due.date().isoformat()
            claim_token = self.schedules.claim_due(schedule, local_date, now=current_now)
            if claim_token is None:
                # Another concurrent tick or process already claimed this date.
                continue

            engine = self._engine_factory()
            due_utc_iso = format_utc(due.astimezone(UTC))
            try:
                task_id = engine.create_task(
                    schedule.repo_id,
                    schedule.task_type,
                    schedule.prompt,
                    scheduled_at=due_utc_iso,
                )
            except Exception as exc:
                # Task creation failed before a task exists. Release the claim so
                # the schedule is not permanently stuck.
                self.schedules.release_claim(schedule, claim_token, local_date)
                message = str(exc)[:1000]
                logger.exception("failed to create task for schedule %s: %s", schedule.id, message)
                self.audit.record(
                    event_type="scheduler.schedule",
                    action="trigger",
                    actor="system",
                    target=schedule.id,
                    outcome=AuditOutcome.FAILURE,
                    detail={
                        "error": message,
                        "repo_id": schedule.repo_id,
                        "local_date": local_date,
                        "task_type": schedule.task_type.value,
                    },
                )
                triggered.append(
                    {
                        "schedule_id": schedule.id,
                        "status": "failed",
                        "error": message,
                        "local_date": local_date,
                    }
                )
                continue

            self._log(
                task_id,
                "task.schedule_triggered",
                {"schedule_id": schedule.id, "local_date": local_date},
            )
            self.audit.record(
                event_type="scheduler.schedule",
                action="trigger",
                actor="system",
                target=schedule.id,
                outcome=AuditOutcome.SUCCESS,
                detail={
                    "task_id": task_id,
                    "repo_id": schedule.repo_id,
                    "task_type": schedule.task_type.value,
                    "local_date": local_date,
                },
            )
            triggered.append(
                {
                    "schedule_id": schedule.id,
                    "status": "triggered",
                    "task_id": task_id,
                    "local_date": local_date,
                }
            )
        return triggered

    def tick(self, *, now: datetime | None = None) -> dict[str, object]:
        """Run recurring schedule checks and at most ``batch_size`` due tasks."""
        if not self._tick_lock.acquire(blocking=False):
            return {"skipped": "a scheduler tick is already running", "results": []}
        results: list[dict[str, object]] = []
        current_now = now or datetime.now(UTC)
        effective_now = format_utc(current_now)
        triggered: list[dict[str, object]] = []
        try:
            triggered = self.trigger_due_schedules(now=current_now)
            for task_id in self.due_task_ids(now=effective_now):
                results.append(self._run_one(task_id))
        finally:
            self._tick_lock.release()
        return {
            "results": results,
            "count": len(results),
            "triggered_schedules": triggered,
        }

    def _run_one(self, task_id: str) -> dict[str, object]:
        engine = self._engine_factory()
        task = engine.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "status": "missing"}
        if task["state"] != TaskState.QUEUED.value:
            return {"task_id": task_id, "status": "not_queued", "state": task["state"]}

        try:
            task_type = TaskType(task["task_type"])
        except ValueError:
            return {"task_id": task_id, "status": "invalid_task_type"}

        if self.dry_run and task_type not in READ_ONLY_TASK_TYPES:
            if self.supervised_automation_enabled:
                self._log(
                    task_id,
                    "task.scheduler.supervised_run",
                    {"reason": "supervised_automation_enabled"},
                )
            else:
                self._log(
                    task_id,
                    "task.scheduler.skipped",
                    {"reason": "dry_run", "scheduled_at_cleared": True},
                )
                # A dry-run write task can never execute while dry-run is on,
                # so leaving scheduled_at in the past would re-select the task
                # on every tick forever. Clear it; the task stays queued and
                # the user can run it manually once dry-run is lifted.
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET scheduled_at = NULL, updated_at = ? "
                        "WHERE id = ? AND state = ?",
                        (_now(), task_id, TaskState.QUEUED.value),
                    )
                return {
                    "task_id": task_id,
                    "status": "skipped",
                    "reason": "dry_run",
                    "scheduled_at_cleared": True,
                }

        self._log(task_id, "task.scheduler.started", {})
        try:
            engine.run_task(task_id)
        except GitBotError as exc:
            message = exc.message[:1000]
            self._log(
                task_id,
                "task.scheduler.failed",
                {"error": message, "code": exc.code},
            )
            self.audit.record(
                event_type="scheduler.run",
                action="run_due_task",
                actor="system",
                target=task_id,
                outcome=AuditOutcome.FAILURE,
                detail={"error": message},
            )
            return {"task_id": task_id, "status": "failed", "error": message}
        except Exception as exc:  # defensive: keep the scheduler loop alive
            message = str(exc)[:1000]
            logger.exception("scheduler failed to run task %s", task_id)
            self._log(task_id, "task.scheduler.failed", {"error": message})
            return {"task_id": task_id, "status": "failed", "error": message}

        refreshed = engine.get_task(task_id) or {}
        self.audit.record(
            event_type="scheduler.run",
            action="run_due_task",
            actor="system",
            target=task_id,
            outcome=AuditOutcome.SUCCESS,
            detail={"state": refreshed.get("state")},
        )
        return {"task_id": task_id, "status": "ran", "state": refreshed.get("state")}

    def _log(self, task_id: str, event_type: str, detail: dict[str, object]) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, ?, 'system', ?)",
                (task_id, _now(), event_type, json.dumps(redact_value(detail))),
            )

    # -- background loop -------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="gitbot-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("scheduler started with a %ss interval", self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:  # pragma: no cover - defensive loop guard
                logger.exception("scheduler tick failed")
