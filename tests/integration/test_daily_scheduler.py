"""Integration tests for recurring daily schedules with mocked AI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.models import TaskState, TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig
from gitbot.repos import RepoStore
from gitbot.tasks.engine import TaskEngine
from gitbot.tasks.scheduler import Scheduler
from gitbot.tasks.schedules import ScheduleStore


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _fake_prompt(prompt: str, **directives: object) -> str:
    if not directives:
        return prompt
    return prompt + " GITBOT_FAKE:" + json.dumps(directives) + "]]"


def _build(
    tmp_path: Path,
    write_fake_opencode: Callable[..., Path],
    *,
    dry_run: bool = False,
) -> tuple[Database, Scheduler, int, Path]:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    repo = RepoStore(db).add(path=str(repo_dir), slug="local/repo", allow_commit=True)

    fake = write_fake_opencode()
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=20))

    def engine_factory() -> TaskEngine:
        return TaskEngine(
            db,
            audit,
            opencode=adapter,
            dry_run=dry_run,
            allowed_roots=(tmp_path,),
            verify_opencode_config=False,
        )

    scheduler = Scheduler(
        db,
        audit,
        dry_run=dry_run,
        allowed_roots=(tmp_path,),
        engine_factory=engine_factory,
        interval_s=30,
        batch_size=5,
        retry_delay_s=0,
    )
    return db, scheduler, repo.id, repo_dir


def test_daily_schedule_creates_runs_and_stops_at_approval(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db, scheduler, repo_id, _repo_dir = _build(tmp_path, write_fake_opencode, dry_run=False)
    store = ScheduleStore(db)
    prompt = _fake_prompt("improve docs", writes={"docs.md": "updated\n"})
    schedule = store.create(
        repo_id,
        TaskType.DOCS,
        prompt,
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )

    result = scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    assert result["triggered_schedules"][0]["schedule_id"] == schedule.id
    assert result["triggered_schedules"][0]["status"] == "triggered"
    task_id = result["triggered_schedules"][0]["task_id"]
    assert result["count"] == 1
    assert result["results"][0]["task_id"] == task_id
    assert result["results"][0]["state"] == TaskState.AWAITING_REVIEW.value

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task is not None
    assert task["state"] == TaskState.AWAITING_REVIEW.value
    assert task["diff_hash"]
    assert task["approval_id"]
    assert task["sandboxed"] == 1
    assert task["diff_patch"]
    assert "docs.md" in task["diff_patch"]

    refreshed = store.get(schedule.id)
    assert refreshed is not None
    assert refreshed.last_run_date == "2026-01-10"

    # No duplicate on the same local day.
    second = scheduler.tick(now=datetime(2026, 1, 10, 10, 0, tzinfo=UTC))
    assert second["triggered_schedules"] == []
    assert second["count"] == 0
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE prompt = ?", (prompt,)
        ).fetchone()["n"]
    assert count == 1


def test_daily_schedule_persists_across_restart_simulation(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db_path = tmp_path / "gitbot.db"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    # First "process": create schedule and run once.
    db1 = Database(db_path)
    db1.migrate()
    audit1 = AuditLog(db1)
    repo = RepoStore(db1).add(path=str(repo_dir), slug="local/repo", allow_commit=True)
    fake = write_fake_opencode()
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=20))
    scheduler1 = Scheduler(
        db1,
        audit1,
        dry_run=False,
        allowed_roots=(tmp_path,),
        engine_factory=lambda: TaskEngine(
            db1, audit1, opencode=adapter, dry_run=False, allowed_roots=(tmp_path,)
        ),
    )
    schedule = ScheduleStore(db1).create(
        repo.id,
        TaskType.ANALYZE,
        _fake_prompt("analyze"),
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )
    scheduler1.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))

    # Restart: new DB handle, new scheduler, same file.
    db2 = Database(db_path)
    audit2 = AuditLog(db2)
    scheduler2 = Scheduler(
        db2,
        audit2,
        dry_run=False,
        allowed_roots=(tmp_path,),
        engine_factory=lambda: TaskEngine(
            db2, audit2, opencode=adapter, dry_run=False, allowed_roots=(tmp_path,)
        ),
    )
    # Same day must not duplicate.
    again = scheduler2.tick(now=datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    assert again["triggered_schedules"] == []
    # Next day runs once.
    next_day = scheduler2.tick(now=datetime(2026, 1, 11, 9, 5, tzinfo=UTC))
    assert len(next_day["triggered_schedules"]) == 1
    assert next_day["triggered_schedules"][0]["schedule_id"] == schedule.id


def test_schedule_respects_repo_tests_and_exact_diff_approval_binding(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db, scheduler, repo_id, _repo_dir = _build(tmp_path, write_fake_opencode, dry_run=False)
    RepoStore(db).update(repo_id, test_command='python3 -c "import sys; sys.exit(0)"')
    store = ScheduleStore(db)
    prompt = _fake_prompt("fix docs", writes={"docs.md": "fixed\n"})
    schedule = store.create(
        repo_id,
        TaskType.DOCS,
        prompt,
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )

    scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        approval = conn.execute(
            "SELECT * FROM approvals WHERE task_id = ?", (task["id"],)
        ).fetchone()
    assert task["tests_status"] == "passed"
    assert task["test_exit_code"] == 0
    assert approval["diff_hash"] == task["diff_hash"]
    assert task["state"] == TaskState.AWAITING_REVIEW.value
    assert schedule.id


def test_failed_tests_prevent_approval_and_commit_flow(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db, scheduler, repo_id, _repo_dir = _build(tmp_path, write_fake_opencode, dry_run=False)
    RepoStore(db).update(repo_id, test_command='python3 -c "import sys; sys.exit(3)"')
    store = ScheduleStore(db)
    store.create(
        repo_id,
        TaskType.DOCS,
        _fake_prompt("break docs", writes={"docs.md": "broken\n"}),
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )
    scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
    assert task["state"] == TaskState.AWAITING_REVIEW.value
    assert task["tests_status"] == "failed"

    # Engine commit guard refuses failed tests (defense in depth); API also
    # refuses approval/commit when tests fail.
    engine = TaskEngine(
        db,
        AuditLog(db),
        dry_run=False,
        allowed_roots=(tmp_path,),
        verify_opencode_config=False,
    )
    from gitbot.tasks.approvals import ApprovalStore

    approval_store = ApprovalStore(db)
    with db.connect() as conn:
        approval_id = task["approval_id"]
    # Force state to approved to verify commit guard (not possible via API).
    with db.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.APPROVED.value, task["id"]),
        )
    import pytest

    from gitbot.errors import PolicyViolation

    with pytest.raises(PolicyViolation, match="did not pass"):
        engine.commit_task(task["id"])
    assert approval_id
    assert approval_store.get(approval_id) is not None
