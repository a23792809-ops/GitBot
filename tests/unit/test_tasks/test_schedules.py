"""Schedule store, timezone, and due-calculation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gitbot.db import Database
from gitbot.errors import NotFoundError, PolicyViolation
from gitbot.models import TaskType
from gitbot.repos import RepoStore
from gitbot.tasks.schedules import (
    ScheduleStore,
    detect_local_timezone_name,
    due_occurrence,
    next_run_at,
    normalize_local_time,
    timezone_is_valid,
)


def _database(tmp_path: Path) -> tuple[Database, int]:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = RepoStore(db).add(path=str(repo_dir), slug="local/repo")
    return db, repo.id


def test_detect_local_timezone_accepts_tz_env(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    assert detect_local_timezone_name() == "America/New_York"


def test_timezone_validation_and_time_format() -> None:
    assert timezone_is_valid("Asia/Kolkata")
    assert timezone_is_valid("localtime")
    assert not timezone_is_valid("Mars/Olympus")
    assert normalize_local_time("09:30") == "09:30"
    with pytest.raises(PolicyViolation):
        normalize_local_time("25:00")


def test_schedule_create_update_pause_resume_delete(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    created = store.create(
        repo_id,
        TaskType.DOCS,
        "Improve docs",
        "08:00",
        timezone="Asia/Kolkata",
        now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    )
    assert created.enabled is True
    assert created.local_time == "08:00"
    assert created.timezone == "Asia/Kolkata"

    updated = store.update(
        created.id,
        task_type=TaskType.EXPLAIN,
        prompt="Explain code",
        local_time="09:15",
        now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    )
    assert updated.task_type is TaskType.EXPLAIN
    assert updated.prompt == "Explain code"
    assert updated.local_time == "09:15"

    paused = store.set_enabled(created.id, False)
    assert paused.enabled is False
    resumed = store.set_enabled(created.id, True)
    assert resumed.enabled is True

    assert store.delete(created.id) is True
    assert store.get(created.id) is None


def test_multiple_schedules_per_repo(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    first = store.create(repo_id, TaskType.ANALYZE, "Analyze", "08:00", timezone="UTC")
    second = store.create(repo_id, TaskType.DOCS, "Document", "09:00", timezone="UTC")
    schedules = store.list(repo_id=repo_id)
    assert {item.id for item in schedules} == {first.id, second.id}


def test_unknown_repo_or_disabled_task_type_rejected(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    with db.connect() as conn:
        conn.execute("UPDATE repos SET allowed_task_types = 'analyze' WHERE id = ?", (repo_id,))
    store = ScheduleStore(db)
    with pytest.raises(NotFoundError):
        store.create(9999, TaskType.ANALYZE, "x", "08:00", timezone="UTC")
    with pytest.raises(PolicyViolation):
        store.create(repo_id, TaskType.DOCS, "x", "08:00", timezone="UTC")


def test_dst_spring_forward_gap_moves_to_first_valid_time(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    schedule = store.create(
        repo_id,
        TaskType.ANALYZE,
        "Analyze",
        "02:30",
        timezone="America/New_York",
        now=datetime(2026, 3, 7, 12, 0, tzinfo=UTC),
    )
    # 2026-03-08 02:30 local does not exist; next run should be 03:30 EDT (07:30 UTC).
    now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    due = due_occurrence(schedule, now)
    assert due is None
    nxt = next_run_at(schedule, now)
    assert nxt == "2026-03-08T07:30:00Z"


def test_dst_fall_back_runs_once_per_local_date(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    schedule = store.create(
        repo_id,
        TaskType.ANALYZE,
        "Analyze",
        "01:30",
        timezone="America/New_York",
        now=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
    )
    # First 01:30 EDT is 05:30 UTC; ambiguous time should run once.
    now = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    due = due_occurrence(schedule, now)
    assert due is not None
    local_date = due.date().isoformat()
    assert local_date == "2026-11-01"
    token = store.claim_due(schedule, local_date, now=now)
    assert token is not None
    refreshed = store.get(schedule.id)
    assert refreshed is not None
    assert due_occurrence(refreshed, datetime(2026, 11, 1, 7, 0, tzinfo=UTC)) is None
    assert (
        store.claim_due(refreshed, local_date, now=datetime(2026, 11, 1, 7, 0, tzinfo=UTC)) is None
    )


def test_new_schedule_after_today_time_waits_until_next_day(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    # 10:30 UTC in UTC zone is after 09:00 local.
    schedule = store.create(
        repo_id,
        TaskType.ANALYZE,
        "Analyze",
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 10, 30, tzinfo=UTC),
    )
    assert schedule.last_run_date == "2026-01-10"
    assert due_occurrence(schedule, datetime(2026, 1, 10, 11, 0, tzinfo=UTC)) is None
    assert next_run_at(schedule, datetime(2026, 1, 10, 11, 0, tzinfo=UTC)) == "2026-01-11T09:00:00Z"


def test_release_claim_does_not_clobber_a_newer_claim(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    schedule = store.create(
        repo_id,
        TaskType.ANALYZE,
        "Analyze",
        "08:00",
        timezone="Europe/Berlin",
        now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    )
    local_date = "2026-06-01"
    token = store.claim_due(schedule, local_date, now=datetime(2026, 6, 1, 7, 0, tzinfo=UTC))
    assert token is not None
    # Simulate a concurrent edit retiming the claim: the release must now refuse.
    with db.connect() as conn:
        conn.execute(
            "UPDATE repo_schedules SET last_run_date = ? WHERE id = ?",
            ("2026-06-02", schedule.id),
        )
    assert store.release_claim(schedule, token, local_date) is False
    refreshed = store.get(schedule.id)
    assert refreshed is not None
    assert refreshed.last_run_date == "2026-06-02"


def test_release_claim_restores_pre_claim_state_when_untouched(tmp_path: Path) -> None:
    db, repo_id = _database(tmp_path)
    store = ScheduleStore(db)
    schedule = store.create(
        repo_id,
        TaskType.ANALYZE,
        "Analyze",
        "08:00",
        timezone="Europe/Berlin",
        now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    )
    assert schedule.last_run_date is None
    local_date = "2026-06-01"
    token = store.claim_due(schedule, local_date, now=datetime(2026, 6, 1, 7, 0, tzinfo=UTC))
    assert token is not None
    assert store.release_claim(schedule, token, local_date) is True
    refreshed = store.get(schedule.id)
    assert refreshed is not None
    assert refreshed.last_run_date is None
