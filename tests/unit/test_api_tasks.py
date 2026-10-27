"""API tests for tasks and approvals."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import load_settings
from gitbot.db import Database
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.repos import RepoStore


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
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)


@contextmanager
def build_client(tmp_path: Path, fake_gh: Path) -> Iterator[tuple[TestClient, Database]]:
    settings = load_settings(env={"HOME": str(tmp_path)})
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    client = GhClient(str(fake_gh), timeout_s=10)
    service = GithubService(client, audit=audit, ttl_s=60)
    app = create_app(settings=settings, database=database, github=service, audit=audit)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, database


def test_create_task_invalid_input(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, _db):
        token_file = tmp_path / ".config" / "gitbot" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("x" * 43 + "\n")
        token_file.chmod(0o600)
        # no token
        r = client.post("/api/tasks", json={"repo_id": 1, "prompt": "hi"})
        assert r.status_code == 401
        # invalid
        r = client.post(
            "/api/tasks",
            json={"repo_id": "x", "prompt": "hi"},
            headers={"Authorization": "Bearer " + "x" * 43},
        )
        assert r.status_code == 422
        r = client.post(
            "/api/tasks", json={"repo_id": 1}, headers={"Authorization": "Bearer " + "x" * 43}
        )
        assert r.status_code == 422


def test_list_and_get_task(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        token_file = tmp_path / ".config" / "gitbot" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("x" * 43 + "\n")
        token_file.chmod(0o600)
        store = RepoStore(db)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_git_repo(repo_dir)
        repo = store.add(path=str(repo_dir), slug="local/r")
        r = client.post(
            "/api/tasks",
            json={"repo_id": repo.id, "task_type": "analyze", "prompt": "do it"},
            headers={"Authorization": "Bearer " + "x" * 43},
        )
        assert r.status_code == 200
        task_id = r.json()["task"]["id"]
        r2 = client.get("/api/tasks")
        assert r2.status_code == 200
        r3 = client.get(f"/api/tasks/{task_id}")
        assert r3.status_code == 200
        r4 = client.get("/api/tasks/nonexistent")
        assert r4.status_code == 404


def test_run_task_against_allowlisted_repo(tmp_path: Path, fake_gh: Path, monkeypatch) -> None:
    fake = _write_fake_opencode(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    (tmp_path / "opencode").write_text(fake.read_text())
    (tmp_path / "opencode").chmod(0o755)
    with build_client(tmp_path, fake_gh) as (client, db):
        token_file = tmp_path / ".config" / "gitbot" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("x" * 43 + "\n")
        token_file.chmod(0o600)
        store = RepoStore(db)
        repo_dir = tmp_path / "repo2"
        repo_dir.mkdir()
        _init_git_repo(repo_dir)
        repo = store.add(path=str(repo_dir), slug="local/r2")
        r = client.post(
            "/api/tasks",
            json={"repo_id": repo.id, "task_type": "analyze", "prompt": "do it"},
            headers={"Authorization": "Bearer " + "x" * 43},
        )
        task_id = r.json()["task"]["id"]
        r2 = client.post(
            f"/api/tasks/{task_id}/run", headers={"Authorization": "Bearer " + "x" * 43}
        )
        assert r2.status_code == 200
        assert r2.json()["task"]["state"] == "awaiting_review"


def test_approvals_pending_and_workflow(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, _db):
        token_file = tmp_path / ".config" / "gitbot" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("x" * 43 + "\n")
        token_file.chmod(0o600)
        r = client.get("/api/tasks/approvals/pending")
        assert r.status_code == 200
        # approve non-existent
        r = client.post(
            "/api/tasks/approvals/nonexist/approve",
            json={"diff_hash": "x"},
            headers={"Authorization": "Bearer " + "x" * 43},
        )
        assert r.status_code in (404, 403)
        r = client.post(
            "/api/tasks/approvals/nonexist/reject",
            headers={"Authorization": "Bearer " + "x" * 43},
        )
        assert r.status_code in (404, 403)
