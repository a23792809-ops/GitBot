"""Cross-process cancellation: another process asks to stop via the database.

The engine runs a watchdog thread for every active run. A second process that
shares the same SQLite database (e.g. a second GitBot server instance) can
request cancellation by inserting a ``task.cancel_requested`` event; the owning
process observes it and raises its in-memory cancel signal. Requests older
than the run's ``started_at`` are ignored so a stale request can never kill a
new run.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.models import TaskType
from gitbot.repos import RepoStore
from gitbot.tasks import engine as engine_module
from gitbot.tasks.engine import TaskEngine


def _setup(tmp_path: Path) -> tuple[Database, TaskEngine, int]:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = RepoStore(db).add(path=str(repo_dir), slug="local/repo")
    # tmp_path lives outside $HOME, so widen the allowlist for this unit test.
    engine = TaskEngine(
        db,
        audit,
        dry_run=True,
        allowed_roots=[tmp_path],
        forbidden_roots=[],
    )
    return db, engine, repo.id


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_running_task(engine: TaskEngine, repo_id: int) -> str:
    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "do things")
    with engine.db.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = 'running', started_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), task_id),
        )
    return task_id


def test_cancel_request_from_another_process_is_recorded(tmp_path: Path) -> None:
    db, engine, repo_id = _setup(tmp_path)
    task_id = _make_running_task(engine, repo_id)

    returned = engine.cancel_task(task_id)

    assert returned["id"] == task_id
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT actor, detail FROM task_events"
            " WHERE task_id = ? AND event_type = 'task.cancel_requested'",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor"] == "user"
    assert json.loads(rows[0]["detail"])["cross_process"] is True


def test_cancel_request_from_another_process_is_idempotent(tmp_path: Path) -> None:
    db, engine, repo_id = _setup(tmp_path)
    task_id = _make_running_task(engine, repo_id)

    engine.cancel_task(task_id)
    engine.cancel_task(task_id)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND event_type = 'task.cancel_requested'",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1


def test_watchdog_honours_database_cancel_request(tmp_path: Path) -> None:
    db, engine, repo_id = _setup(tmp_path)
    task_id = _make_running_task(engine, repo_id)
    started = str(engine.get_task(task_id)["started_at"])

    cancel_event = threading.Event()
    stop = threading.Event()
    watcher = threading.Thread(
        target=engine_module._watch_cancel_requests,
        args=(db, task_id, cancel_event, started, stop),
        daemon=True,
    )
    watcher.start()
    try:
        engine.cancel_task(task_id)
        deadline = time.time() + 3.0
        while time.time() < deadline and not cancel_event.wait(0.05):
            pass
        assert cancel_event.is_set(), "the owning process must observe the database request"
    finally:
        stop.set()
        watcher.join(timeout=2.0)


def test_watchdog_ignores_requests_from_before_the_run(tmp_path: Path) -> None:
    db, engine, repo_id = _setup(tmp_path)
    task_id = _make_running_task(engine, repo_id)
    started = str(engine.get_task(task_id)["started_at"])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, created_at, event_type, actor, detail)"
            " VALUES (?, '2000-01-01T00:00:00Z', 'task.cancel_requested', 'user', '{}')",
            (task_id,),
        )

    cancel_event = threading.Event()
    stop = threading.Event()
    watcher = threading.Thread(
        target=engine_module._watch_cancel_requests,
        args=(db, task_id, cancel_event, started, stop),
        daemon=True,
    )
    watcher.start()
    try:
        time.sleep(1.2)
        assert not cancel_event.is_set(), "a stale request must never cancel a new run"
    finally:
        stop.set()
        watcher.join(timeout=2.0)


def test_in_process_cancel_path_is_unaffected(tmp_path: Path) -> None:
    db, engine, repo_id = _setup(tmp_path)
    task_id = _make_running_task(engine, repo_id)
    # Register the task as if this process owns the run.
    cancel_event = engine_module._register_cancel(task_id)
    try:
        returned = engine.cancel_task(task_id)
        assert returned["id"] == task_id
        assert cancel_event.is_set(), "same-process cancel must remain immediate"
    finally:
        engine_module._release_cancel(task_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND event_type = 'task.cancel_requested'",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1
