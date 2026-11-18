"""Disposable-profile integration tests.

These tests prove the isolated test profile starts from migration 0001, can
create and update tasks/approvals/history, survives a simulated restart, and
never touches the real GitBot database or copies the real token.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database
from gitbot.models import ApprovalAction, TaskState, TaskType
from gitbot.repos import RepoStore
from gitbot.security.tokens import read_token
from gitbot.tasks.approvals import ApprovalStore, compute_diff_hash
from gitbot.tasks.engine import TaskEngine, recover_stale_tasks

EXPECTED_MIGRATIONS = [
    "0001_init",
    "0002_tasks",
    "0003_task_output",
    "0004_task_review",
    "0005_analysis_state",
    "0006_explain_scheduler_tests",
    "0007_daily_schedules",
]
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_test_profile.sh"


def _profile_env(profile: Path) -> dict[str, str]:
    return {
        "HOME": str(profile / "home"),
        "XDG_DATA_HOME": str(profile / "data"),
        "XDG_CONFIG_HOME": str(profile / "config"),
    }


@pytest.fixture
def disposable_settings(tmp_path: Path) -> Iterator[Settings]:
    profile = tmp_path / "profile"
    for name in ("home", "data", "config"):
        (profile / name).mkdir(parents=True, mode=0o700)
    settings = load_settings(env=_profile_env(profile))
    assert settings.db_path.is_relative_to(profile)
    assert settings.token_file.is_relative_to(profile)
    yield settings


def _real_db_state() -> tuple[int, int] | None:
    """Read-only metadata for the real profile DB; never opens or migrates it."""
    real_db = Path(os.environ.get("HOME", "~")).expanduser() / ".local/share/gitbot/gitbot.db"
    try:
        stat = real_db.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def test_disposable_profile_applies_every_migration(disposable_settings: Settings) -> None:
    before = _real_db_state()
    database = Database(disposable_settings.db_path)
    applied = database.migrate()

    assert applied == EXPECTED_MIGRATIONS
    assert database.applied_migrations()
    # Running migrate again is idempotent and applies nothing.
    assert database.migrate() == []
    assert _real_db_state() == before


def test_disposable_profile_supports_tasks_approvals_and_history(
    disposable_settings: Settings,
) -> None:
    before = _real_db_state()
    database = Database(disposable_settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    repo_dir = Path(disposable_settings.home) / "projects" / "demo"
    repo_dir.mkdir(parents=True)
    repo = RepoStore(database).add(path=str(repo_dir), slug="local/demo")

    engine = TaskEngine(
        database,
        audit,
        dry_run=True,
        allowed_roots=(Path(disposable_settings.home),),
    )
    task_id = engine.create_task(repo.id, TaskType.ANALYZE, "explain the README")
    task = engine.get_task(task_id)
    assert task is not None
    assert task["state"] == TaskState.QUEUED.value

    # Move the task through review with a diff-hash-bound approval.
    patch = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-hi\n+hello\n"
    diff_hash = compute_diff_hash(patch)
    with database.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = ?, sandboxed = 1, snapshot_head = ?, diff_patch = ?, "
            "diff_hash = ?, updated_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (TaskState.AWAITING_REVIEW.value, "a" * 40, patch, diff_hash, task_id),
        )
    approval = ApprovalStore(database).create(
        task_id,
        repo.id,
        ApprovalAction.COMMIT,
        diff_hash,
        ["git", "commit"],
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = ?, approval_id = ? WHERE id = ?",
            (TaskState.APPROVED.value, approval.id, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
            "VALUES (?, '2026-01-01T00:00:00Z', 'task.approved', 'user', '{}')",
            (task_id,),
        )

    # A fresh Database instance is the restart boundary: history is durable.
    reopened = Database(disposable_settings.db_path)
    with reopened.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        events = conn.execute(
            "SELECT event_type FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchall()
    assert row["state"] == TaskState.APPROVED.value
    assert {event["event_type"] for event in events} >= {"task.created", "task.approved"}

    # An approval must not survive a simulated restart mid-commit.
    with reopened.connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
            "VALUES (?, '2026-01-01T00:00:00Z', 'task.commit_started', 'system', '{}')",
            (task_id,),
        )
    assert task_id in recover_stale_tasks(reopened)
    with reopened.connect() as conn:
        recovered = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        used = conn.execute("SELECT used_at FROM approvals WHERE id = ?", (approval.id,)).fetchone()
    assert recovered["state"] == TaskState.FAILED.value
    assert used["used_at"]

    # The disposable profile never gains the real token.
    assert read_token(disposable_settings.token_file) is None
    assert _real_db_state() == before


def test_test_profile_script_cannot_touch_the_real_database(tmp_path: Path) -> None:
    profile = tmp_path / "script-profile"
    before = _real_db_state()
    env = {
        **os.environ,
        "GITBOT_TEST_PROFILE": str(profile),
        "GITBOT_TEST_PYTHON": sys.executable,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert str(profile) in result.stdout
    assert (profile / "data" / "gitbot" / "gitbot.db").is_file()
    assert not (profile / "config" / "gitbot" / "token").exists()
    assert _real_db_state() == before


def test_test_profile_script_refuses_the_real_profile(tmp_path: Path) -> None:
    real_data = Path(os.environ.get("HOME", "~")).expanduser() / ".local/share/gitbot"
    env = {
        **os.environ,
        "GITBOT_TEST_PROFILE": str(real_data),
        "GITBOT_TEST_PYTHON": sys.executable,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing" in result.stderr
