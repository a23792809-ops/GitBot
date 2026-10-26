"""Mutating allowlist endpoints: auth gate, policy enforcement, and audit."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.security.tokens import ensure_token


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


@contextmanager
def build(
    settings: Settings, fake_gh: Path, *, with_token: bool = True
) -> Iterator[tuple[TestClient, Database, AuditLog, str | None]]:
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    token = ensure_token(settings.token_file) if with_token else None
    app = create_app(
        settings=settings,
        database=database,
        github=GithubService(GhClient(str(fake_gh)), audit=audit),
        audit=audit,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, database, audit, token


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env={"HOME": str(tmp_path)})


@pytest.fixture
def repo_path(settings: Settings) -> Path:
    return _git_repo(settings.home / "projects" / "sample")


@contextmanager
def api(settings: Settings, fake_gh: Path, *, with_token: bool = True):
    with build(settings, fake_gh, with_token=with_token) as bundle:
        yield bundle


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_add_requires_token(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, _token):
        response = client.post("/api/repos", json={"path": str(repo_path)})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_add_rejects_wrong_token(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, _token):
        response = client.post("/api/repos", json={"path": str(repo_path)}, headers=_auth("x" * 43))

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "invalid API token"


def test_add_succeeds_and_is_audited(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, audit, token):
        response = client.post(
            "/api/repos", json={"path": str(repo_path)}, headers=_auth(token or "")
        )
        entries = audit.list_entries(event_type="repo.allowlist")

    assert response.status_code == 201
    repo = response.json()["repo"]
    assert repo["path"] == str(repo_path.resolve())
    assert repo["slug"] == "local/sample"
    assert repo["allow_commit"] is False
    assert repo["allow_push"] is False

    assert len(entries) == 1
    assert entries[0].action == "add"
    assert entries[0].outcome == "success"
    assert token not in str(entries[0].detail)


def test_add_appears_in_the_read_endpoint(
    settings: Settings, fake_gh: Path, repo_path: Path
) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, token):
        client.post("/api/repos", json={"path": str(repo_path)}, headers=_auth(token or ""))
        listing = client.get("/api/repos")

    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["repos"][0]["slug"] == "local/sample"


def test_duplicate_path_is_conflict(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, token):
        headers = _auth(token or "")
        first = client.post("/api/repos", json={"path": str(repo_path)}, headers=headers)
        second = client.post("/api/repos", json={"path": str(repo_path)}, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_non_git_path_is_denied_and_audited(settings: Settings, fake_gh: Path) -> None:
    plain = settings.home / "plain"
    plain.mkdir()

    with api(settings, fake_gh) as (client, _db, audit, token):
        response = client.post("/api/repos", json={"path": str(plain)}, headers=_auth(token or ""))
        entries = audit.list_entries(event_type="repo.allowlist")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_not_allowed"
    assert entries[0].outcome == "denied"
    assert entries[0].detail["code"] == "path_not_allowed"


def test_relative_path_is_denied(settings: Settings, fake_gh: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, token):
        response = client.post(
            "/api/repos", json={"path": "relative/repo"}, headers=_auth(token or "")
        )

    assert response.status_code == 403
    assert "absolute" in response.json()["error"]["message"]


def test_update_toggles_permissions(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, audit, token):
        headers = _auth(token or "")
        created = client.post("/api/repos", json={"path": str(repo_path)}, headers=headers)
        repo_id = created.json()["repo"]["id"]

        updated = client.patch(
            f"/api/repos/{repo_id}", json={"allow_commit": True}, headers=headers
        )
        entries = audit.list_entries(event_type="repo.allowlist")

    assert updated.status_code == 200
    assert updated.json()["repo"]["allow_commit"] is True
    assert updated.json()["repo"]["allow_push"] is False
    # Newest first.
    assert [entry.action for entry in entries] == ["update", "add"]


def test_delete_removes_repo(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, audit, token):
        headers = _auth(token or "")
        repo_id = client.post("/api/repos", json={"path": str(repo_path)}, headers=headers).json()[
            "repo"
        ]["id"]

        deleted = client.delete(f"/api/repos/{repo_id}", headers=headers)
        listing = client.get("/api/repos")
        entries = audit.list_entries(event_type="repo.allowlist")

    assert deleted.status_code == 204
    assert listing.json()["count"] == 0
    assert "remove" in [entry.action for entry in entries]


def test_delete_unknown_repo_is_404(settings: Settings, fake_gh: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, token):
        response = client.delete("/api/repos/999", headers=_auth(token or ""))
    assert response.status_code == 404


def test_extra_fields_are_rejected(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, token):
        response = client.post(
            "/api/repos",
            json={"path": str(repo_path), "allow_admin": True},
            headers=_auth(token or ""),
        )
    assert response.status_code == 422


def test_unsafe_branch_name_is_rejected(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    with api(settings, fake_gh) as (client, _db, _audit, token):
        response = client.post(
            "/api/repos",
            json={"path": str(repo_path), "default_branch": "../evil"},
            headers=_auth(token or ""),
        )
    assert response.status_code == 422


def test_missing_token_file_yields_actionable_error(
    settings: Settings, fake_gh: Path, repo_path: Path
) -> None:
    with api(settings, fake_gh, with_token=False) as (client, _db, _audit, _token):
        response = client.post("/api/repos", json={"path": str(repo_path)}, headers=_auth("y" * 43))

    assert response.status_code == 401
    assert "rotate-token" in response.json()["error"]["details"]["hint"]
