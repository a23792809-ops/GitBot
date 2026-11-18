"""Schedule API tests."""

from __future__ import annotations

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

TOKEN = "x" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


@contextmanager
def build_client(tmp_path: Path, fake_gh: Path) -> Iterator[tuple[TestClient, Database]]:
    settings = load_settings(
        env={
            "HOME": str(tmp_path),
            "GITBOT_AI_GATEWAY_ENABLED": "false",
            "GITBOT_DRY_RUN": "true",
            "GITBOT_SCHEDULER_ENABLED": "false",
        }
    )
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    service = GithubService(GhClient(str(fake_gh), timeout_s=10), audit=audit, ttl_s=60)
    app = create_app(settings=settings, database=database, github=service, audit=audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, database


def _write_token(tmp_path: Path) -> None:
    token_file = tmp_path / ".config" / "gitbot" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)


def _seed_repo(tmp_path: Path, db: Database) -> int:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    return RepoStore(db).add(path=str(repo_dir), slug="local/repo").id


def test_schedule_endpoints_require_auth(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, _db):
        assert client.get("/api/schedules").status_code == 401
        assert client.post("/api/schedules", json={}).status_code == 401


def test_schedule_crud_pause_resume_delete(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)

        created = client.post(
            "/api/schedules",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": "Improve docs daily",
                "local_time": "09:00",
                "timezone": "Asia/Kolkata",
                "enabled": True,
            },
            headers=HEADERS,
        )
        assert created.status_code == 201, created.json()
        schedule = created.json()["schedule"]
        schedule_id = schedule["id"]
        assert schedule["repo_id"] == repo_id
        assert schedule["task_type"] == "docs"
        assert schedule["enabled"] is True

        listed = client.get("/api/schedules", headers=HEADERS)
        assert listed.status_code == 200
        assert listed.json()["count"] == 1

        got = client.get(f"/api/schedules/{schedule_id}", headers=HEADERS)
        assert got.status_code == 200
        assert got.json()["schedule"]["id"] == schedule_id

        updated = client.patch(
            f"/api/schedules/{schedule_id}",
            json={"prompt": "Updated prompt", "local_time": "10:30", "task_type": "explain"},
            headers=HEADERS,
        )
        assert updated.status_code == 200
        assert updated.json()["schedule"]["prompt"] == "Updated prompt"
        assert updated.json()["schedule"]["local_time"] == "10:30"
        assert updated.json()["schedule"]["task_type"] == "explain"

        paused = client.post(f"/api/schedules/{schedule_id}/pause", headers=HEADERS)
        assert paused.status_code == 200
        assert paused.json()["schedule"]["enabled"] is False
        resumed = client.post(f"/api/schedules/{schedule_id}/resume", headers=HEADERS)
        assert resumed.status_code == 200
        assert resumed.json()["schedule"]["enabled"] is True

        history = client.get(f"/api/schedules/{schedule_id}/history", headers=HEADERS)
        assert history.status_code == 200
        assert "history" in history.json()

        deleted = client.delete(f"/api/schedules/{schedule_id}", headers=HEADERS)
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get(f"/api/schedules/{schedule_id}", headers=HEADERS).status_code == 404


def test_schedule_validation_and_policy(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        with db.connect() as conn:
            conn.execute("UPDATE repos SET allowed_task_types = 'analyze' WHERE id = ?", (repo_id,))

        bad_time = client.post(
            "/api/schedules",
            json={
                "repo_id": repo_id,
                "task_type": "analyze",
                "prompt": "Analyze",
                "local_time": "25:61",
            },
            headers=HEADERS,
        )
        assert bad_time.status_code == 422

        bad_type = client.post(
            "/api/schedules",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": "Docs",
                "local_time": "09:00",
            },
            headers=HEADERS,
        )
        assert bad_type.status_code == 403
