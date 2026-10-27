"""Integration test: task engine with fake opencode in disposable temp repo."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.models import TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig
from gitbot.repos import RepoStore
from gitbot.tasks.engine import TaskEngine


def _write_fake_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "opencode"
    content = (
        "#!/bin/bash\n"
        "cat << 'EOF'\n"
        '{"type":"step_start","timestamp":1,"sessionID":"s1","part":{"type":"step-start"}}\n'
        '{"type":"tool_use","timestamp":2,"sessionID":"s1","part":{"type":"tool","tool":"shell",'
        '"state":{"status":"completed","input":{"command":"git status"},"output":"ok"}}}\n'
        '{"type":"step_finish","timestamp":3,"sessionID":"s1","part":{"type":"step-finish","reason":"tool-calls"}}\n'
        '{"type":"text","timestamp":4,"sessionID":"s1","part":{"type":"text","text":"done"}}\n'
        "EOF\n"
        "exit 0\n"
    )
    script.write_text(content, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)


def test_task_engine_end_to_end_fake(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "gitbot.db"
    db = Database(db_path)
    db.migrate()
    audit = AuditLog(db)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    store = RepoStore(db)
    repo = store.add(path=str(repo_dir), slug="local/repo")
    fake = _write_fake_opencode(tmp_path)
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=10))
    engine = TaskEngine(db, audit, opencode=adapter)
    task_id = engine.create_task(repo.id, TaskType.ANALYZE, "analyze")
    task = engine.run_task(task_id)
    assert task["state"] == "awaiting_review"
    assert task["branch_name"].startswith("gitbot/")
    assert task["diff_hash"] is not None
