"""Supervised daily automation: schedule -> sandboxed change -> tests ->
review -> approval -> local commit.

Uses a disposable DB/repo and mocked OpenCode (fake binary). No live AI, no real
profile, no GitHub writes.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import load_settings
from gitbot.db import Database
from gitbot.errors import PolicyViolation
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.models import TaskState, TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig
from gitbot.repos import RepoStore
from gitbot.tasks.approvals import ApprovalStore
from gitbot.tasks.engine import TaskEngine
from gitbot.tasks.scheduler import Scheduler
from gitbot.tasks.schedules import ScheduleStore

TOKEN = "x" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


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


def _write_token(home: Path) -> None:
    token_file = home / ".config" / "gitbot" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)


def _seed_repo(tmp_path: Path, db: Database, *, allow_commit: bool = True) -> tuple[int, Path]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    repo = RepoStore(db).add(path=str(repo_dir), slug="local/repo", allow_commit=allow_commit)
    return repo.id, repo_dir


def _fake_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_fake_opencode: Callable[..., Path],
) -> Path:
    fake = write_fake_opencode()
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    return fake


def test_supervised_daily_schedule_completes_full_workflow_with_dry_run_true(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_fake_opencode: Callable[..., Path],
) -> None:
    """Full supervised workflow: schedule -> run -> diff/tests -> review -> approve -> commit."""
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    env = {
        "HOME": str(tmp_path),
        "GITBOT_DRY_RUN": "true",
        "GITBOT_SUPERVISED_AUTOMATION_ENABLED": "true",
        "GITBOT_ALLOW_APPROVED_LOCAL_COMMITS": "true",
        "GITBOT_SCHEDULER_ENABLED": "false",
        "GITBOT_AI_GATEWAY_ENABLED": "false",
    }
    settings = load_settings(env=env)
    db = Database(settings.db_path)
    db.migrate()
    audit = AuditLog(db)
    service = GithubService(GhClient(str(fake_gh), timeout_s=10), audit=audit, ttl_s=60)
    app = create_app(settings=settings, database=db, github=service, audit=audit)

    _write_token(tmp_path)
    repo_id, repo_dir = _seed_repo(tmp_path, db)

    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/schedules",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("improve docs", writes={"docs.md": "daily improvement\n"}),
                "local_time": "09:00",
                "timezone": "UTC",
                "enabled": True,
            },
            headers=HEADERS,
        )
        assert created.status_code == 201, created.json()
        schedule_id = created.json()["schedule"]["id"]

    scheduler = Scheduler(
        db,
        audit,
        dry_run=True,
        supervised_automation_enabled=True,
        allow_approved_local_commits=True,
        allowed_roots=(tmp_path,),
        engine_factory=lambda: TaskEngine(
            db,
            audit,
            opencode=OpenCodeAdapter(
                OpenCodeConfig(executable=str(tmp_path / "opencode"), timeout_s=20)
            ),
            dry_run=True,
            supervised_automation_enabled=True,
            allow_approved_local_commits=True,
            allowed_roots=(tmp_path,),
            verify_opencode_config=False,
        ),
    )
    store = ScheduleStore(db)
    sched = store.get(schedule_id)
    assert sched is not None
    if sched.last_run_date is not None:
        with db.connect() as conn:
            conn.execute(
                "UPDATE repo_schedules SET last_run_date = NULL, last_run_at = NULL WHERE id = ?",
                (schedule_id,),
            )
    result = scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    assert result["triggered_schedules"], result
    task_id = result["triggered_schedules"][0]["task_id"]
    assert result["count"] == 1
    assert result["results"][0]["state"] == TaskState.AWAITING_REVIEW.value

    with TestClient(app, raise_server_exceptions=False) as client:
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS)
        assert detail.status_code == 200
        task = detail.json()["task"]
        assert task["state"] == TaskState.AWAITING_REVIEW.value
        assert task["diff_hash"]
        assert task["diff_patch"]
        assert task["sandboxed"] == 1
        assert "docs.md" in task["diff_patch"]
        assert detail.json()["diff"]["stored"] is True

        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200, approved.json()

        committed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert committed.status_code == 200, committed.json()
        assert committed.json()["task"]["state"] == TaskState.COMMITTED.value

    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log.startswith("gitbot: docs task")
    assert (repo_dir / "docs.md").read_text(encoding="utf-8") == "daily improvement\n"


def test_supervised_mode_does_not_auto_approve_or_commit(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_id, _repo_dir = _seed_repo(tmp_path, db)
    fake = write_fake_opencode()
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=20))
    scheduler = Scheduler(
        db,
        audit,
        dry_run=True,
        supervised_automation_enabled=True,
        allow_approved_local_commits=True,
        allowed_roots=(tmp_path,),
        engine_factory=lambda: TaskEngine(
            db,
            audit,
            opencode=adapter,
            dry_run=True,
            supervised_automation_enabled=True,
            allow_approved_local_commits=True,
            allowed_roots=(tmp_path,),
        ),
    )
    store = ScheduleStore(db)
    store.create(
        repo_id,
        TaskType.DOCS,
        _fake_prompt("docs", writes={"docs.md": "x\n"}),
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )
    result = scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    assert result["count"] == 1
    with db.connect() as conn:
        task = conn.execute("SELECT state FROM tasks").fetchone()
    assert task["state"] == TaskState.AWAITING_REVIEW.value


def test_commit_blocked_without_approved_local_commit_flag_in_supervised_dry_run(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_id, _repo_dir = _seed_repo(tmp_path, db)
    fake = write_fake_opencode()
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=20))
    engine = TaskEngine(
        db,
        audit,
        opencode=adapter,
        dry_run=True,
        supervised_automation_enabled=True,
        allow_approved_local_commits=False,
        allowed_roots=(tmp_path,),
    )
    scheduler = Scheduler(
        db,
        audit,
        dry_run=True,
        supervised_automation_enabled=True,
        allow_approved_local_commits=False,
        allowed_roots=(tmp_path,),
        engine_factory=lambda: engine,
    )
    store = ScheduleStore(db)
    store.create(
        repo_id,
        TaskType.DOCS,
        _fake_prompt("docs", writes={"docs.md": "x\n"}),
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )
    scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks").fetchone()
    assert task["state"] == TaskState.AWAITING_REVIEW.value

    ApprovalStore(db)
    with db.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.APPROVED.value, task["id"]),
        )
    with pytest.raises(PolicyViolation, match="refusing to commit"):
        engine.commit_task(task["id"])


def test_write_schedule_not_executed_without_supervised_mode_in_dry_run(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_id, _repo_dir = _seed_repo(tmp_path, db)
    fake = write_fake_opencode()
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=20))
    scheduler = Scheduler(
        db,
        audit,
        dry_run=True,
        supervised_automation_enabled=False,
        allow_approved_local_commits=False,
        allowed_roots=(tmp_path,),
        engine_factory=lambda: TaskEngine(
            db, audit, opencode=adapter, dry_run=True, allowed_roots=(tmp_path,)
        ),
    )
    store = ScheduleStore(db)
    store.create(
        repo_id,
        TaskType.DOCS,
        _fake_prompt("docs", writes={"docs.md": "x\n"}),
        "09:00",
        timezone="UTC",
        now=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
    )
    result = scheduler.tick(now=datetime(2026, 1, 10, 9, 5, tzinfo=UTC))
    assert result["triggered_schedules"]
    assert result["results"][0]["status"] == "skipped"
    assert result["results"][0]["reason"] == "dry_run"
    with db.connect() as conn:
        task = conn.execute("SELECT state FROM tasks").fetchone()
    assert task["state"] == TaskState.QUEUED.value
