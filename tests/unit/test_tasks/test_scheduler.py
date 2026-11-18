"""Scheduler tests: due selection, dry-run safety, and audit events."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import PolicyViolation
from gitbot.models import TaskType
from gitbot.tasks.scheduler import Scheduler


def _database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    now = "2026-01-01T00:00:00Z"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO repos (id, path, slug, created_at, updated_at) "
            "VALUES (1, '/tmp/repo', 'local/repo', ?, ?)",
            (now, now),
        )
    return db


def _task(
    db: Database,
    task_id: str,
    *,
    state: str = "queued",
    task_type: str = "docs",
    scheduled_at: str | None = "2026-01-01T00:00:00Z",
) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, repo_id, task_type, state, prompt, created_at, updated_at, scheduled_at) "
            "VALUES (?, 1, ?, ?, 'run', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?)",
            (task_id, task_type, state, scheduled_at),
        )


class FakeEngine:
    def __init__(
        self,
        db: Database,
        *,
        fail: bool = False,
        fail_create: bool = False,
        fail_message: str = "fake failure",
    ) -> None:
        self.db = db
        self.fail = fail
        self.fail_create = fail_create
        self.fail_message = fail_message
        self.ran: list[str] = []
        self.created: list[str] = []

    def get_task(self, task_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def create_task(
        self,
        repo_id: int,
        task_type: TaskType | str,
        prompt: str,
        model: str | None = None,
        *,
        scheduled_at: str | None = None,
        max_retries: int = 0,
        parent_task_id: str | None = None,
        retry_count: int = 0,
    ) -> str:
        if self.fail_create:
            raise PolicyViolation(self.fail_message)
        task_id = f"tsk_fake_{len(self.created) + 1}"
        self.created.append(task_id)
        type_str = task_type.value if hasattr(task_type, "value") else str(task_type)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, repo_id, task_type, state, prompt, created_at, updated_at, scheduled_at) "
                "VALUES (?, ?, ?, 'queued', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?)",
                (task_id, repo_id, type_str, prompt, scheduled_at),
            )
        return task_id

    def run_task(self, task_id: str) -> dict:
        self.ran.append(task_id)
        if self.fail:
            raise PolicyViolation(self.fail_message)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET state = 'awaiting_review' WHERE id = ?", (task_id,))
        return self.get_task(task_id) or {}


def test_due_task_ids_filters_state_and_time(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _task(db, "due", scheduled_at="2020-01-01T00:00:00Z")
    _task(db, "future", scheduled_at="2030-01-01T00:00:00Z")
    _task(db, "unscheduled", scheduled_at=None)
    _task(db, "running", state="running", scheduled_at="2020-01-01T00:00:00Z")

    scheduler = Scheduler(db, AuditLog(db), dry_run=False)
    assert scheduler.due_task_ids(now="2026-01-01T00:00:00Z") == ["due"]
    assert scheduler.next_scheduled_at() == "2020-01-01T00:00:00Z"


def test_tick_skips_change_tasks_in_dry_run(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _task(db, "docs")
    engine = FakeEngine(db)
    scheduler = Scheduler(db, AuditLog(db), dry_run=True, engine_factory=lambda: engine)

    result = scheduler.tick()

    assert result["count"] == 1
    assert result["results"] == [
        {"task_id": "docs", "status": "skipped", "reason": "dry_run", "scheduled_at_cleared": True}
    ]
    assert engine.ran == []
    with db.connect() as conn:
        event = conn.execute("SELECT event_type FROM task_events WHERE task_id = 'docs'").fetchall()
        scheduled = conn.execute("SELECT scheduled_at FROM tasks WHERE id = 'docs'").fetchone()
    assert [row["event_type"] for row in event] == ["task.scheduler.skipped"]
    assert scheduled["scheduled_at"] is None

    # A second tick must not re-select or re-skip the same write task.
    result = scheduler.tick()
    assert result["results"] == []


def test_tick_runs_read_only_task_and_audits(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _task(db, "explain", task_type="explain")
    engine = FakeEngine(db)
    audit = AuditLog(db)
    scheduler = Scheduler(db, audit, dry_run=True, engine_factory=lambda: engine)

    result = scheduler.tick()

    assert result["results"] == [
        {"task_id": "explain", "status": "ran", "state": "awaiting_review"}
    ]
    assert engine.ran == ["explain"]
    assert any(entry.event_type == "scheduler.run" for entry in audit.list_entries())


def test_tick_records_a_failed_run_without_stopping(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _task(db, "explain", task_type="explain")
    _task(db, "explain2", task_type="explain")
    engine = FakeEngine(db, fail=True)
    scheduler = Scheduler(db, AuditLog(db), dry_run=True, engine_factory=lambda: engine)

    result = scheduler.tick()

    assert engine.ran == ["explain", "explain2"]
    assert [item["status"] for item in result["results"]] == ["failed", "failed"]
    with db.connect() as conn:
        events = conn.execute("SELECT event_type FROM task_events ORDER BY id").fetchall()
    assert [row["event_type"] for row in events].count("task.scheduler.failed") == 2


def test_scheduler_failure_event_redacts_credential_shapes(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _task(db, "explain", task_type="explain")
    secret = "ghp_" + "a" * 30
    engine = FakeEngine(db, fail=True, fail_message=f"provider rejected token={secret}")
    scheduler = Scheduler(db, AuditLog(db), dry_run=True, engine_factory=lambda: engine)

    scheduler.tick()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT detail FROM task_events WHERE event_type = 'task.scheduler.failed'"
        ).fetchone()
    assert secret not in row["detail"]
    assert "[REDACTED:github_token]" in row["detail"]


def test_tick_triggers_due_recurring_schedule_and_executes_task(tmp_path: Path) -> None:
    db = _database(tmp_path)
    engine = FakeEngine(db)
    audit = AuditLog(db)
    scheduler = Scheduler(db, audit, dry_run=False, engine_factory=lambda: engine)

    # Create recurring schedule for repo 1 at 09:00 UTC
    sch = scheduler.schedules.create(
        repo_id=1,
        task_type=TaskType.DOCS,
        prompt="Update documentation daily",
        local_time="09:00",
        timezone="UTC",
    )

    # Tick at 08:30 UTC: not due yet
    now_early = datetime(2026, 5, 10, 8, 30, tzinfo=UTC)
    result_early = scheduler.tick(now=now_early)
    assert result_early["count"] == 0
    assert result_early["triggered_schedules"] == []
    assert engine.created == []

    # Tick at 09:15 UTC: due!
    now_due = datetime(2026, 5, 10, 9, 15, tzinfo=UTC)
    result_due = scheduler.tick(now=now_due)
    assert len(result_due["triggered_schedules"]) == 1
    assert result_due["triggered_schedules"][0]["schedule_id"] == sch.id
    assert result_due["triggered_schedules"][0]["status"] == "triggered"
    assert result_due["count"] == 1
    assert engine.ran == [result_due["triggered_schedules"][0]["task_id"]]

    # Audit log recorded
    entries = audit.list_entries(event_type="scheduler.schedule")
    assert len(entries) >= 1
    assert entries[0].target == sch.id
    assert entries[0].outcome == "success"


def test_recurring_schedule_duplicate_prevention_across_ticks(tmp_path: Path) -> None:
    db = _database(tmp_path)
    engine = FakeEngine(db)
    audit = AuditLog(db)
    scheduler = Scheduler(db, audit, dry_run=False, engine_factory=lambda: engine)

    scheduler.schedules.create(
        repo_id=1,
        task_type=TaskType.EXPLAIN,
        prompt="Explain codebase daily",
        local_time="09:00",
        timezone="UTC",
    )

    # First run on 2026-05-10
    now_day1 = datetime(2026, 5, 10, 9, 30, tzinfo=UTC)
    res1 = scheduler.tick(now=now_day1)
    assert len(res1["triggered_schedules"]) == 1
    assert engine.ran == ["tsk_fake_1"]

    # Second run on same calendar day: must NOT trigger again!
    now_day1_later = datetime(2026, 5, 10, 15, 0, tzinfo=UTC)
    res2 = scheduler.tick(now=now_day1_later)
    assert res2["triggered_schedules"] == []
    assert res2["count"] == 0
    assert engine.ran == ["tsk_fake_1"]

    # Next calendar day: triggers fresh run
    now_day2 = datetime(2026, 5, 11, 9, 5, tzinfo=UTC)
    res3 = scheduler.tick(now=now_day2)
    assert len(res3["triggered_schedules"]) == 1
    assert engine.ran == ["tsk_fake_1", "tsk_fake_2"]


def test_recurring_schedule_claim_released_on_engine_error(tmp_path: Path) -> None:
    db = _database(tmp_path)
    engine = FakeEngine(db, fail_create=True, fail_message="disk full")
    audit = AuditLog(db)
    scheduler = Scheduler(db, audit, dry_run=False, engine_factory=lambda: engine)

    sch = scheduler.schedules.create(
        repo_id=1,
        task_type=TaskType.DOCS,
        prompt="Daily docs",
        local_time="09:00",
        timezone="UTC",
    )

    now = datetime(2026, 5, 10, 9, 30, tzinfo=UTC)
    result = scheduler.tick(now=now)

    assert len(result["triggered_schedules"]) == 1
    assert result["triggered_schedules"][0]["status"] == "failed"
    assert "disk full" in str(result["triggered_schedules"][0]["error"])

    # Claim must be released so schedule is not permanently stuck
    refreshed = scheduler.schedules.get(sch.id)
    assert refreshed is not None
    assert refreshed.last_run_date == sch.last_run_date

    # Failure audited
    entries = audit.list_entries(event_type="scheduler.schedule")
    assert len(entries) >= 1
    assert entries[0].outcome == "failure"
    assert "disk full" in str(entries[0].detail.get("error"))
