"""API tests: real routing + real SQLite, fake `gh` for GitHub reads."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
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


@contextmanager
def build_client(
    settings: Settings,
    fake_gh: Path,
    *,
    ttl_s: int = 60,
) -> Iterator[tuple[TestClient, Database, AuditLog]]:
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    client = GhClient(str(fake_gh), timeout_s=10)
    service = GithubService(client, audit=audit, ttl_s=ttl_s)
    app = create_app(settings=settings, database=database, github=service, audit=audit)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, database, audit


@pytest.fixture
def api(tmp_path: Path, fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    @contextmanager
    def factory(scenario: str = "ok", *, ttl_s: int = 60):
        monkeypatch.setenv("FAKE_GH_SCENARIO", scenario)
        settings = load_settings(env={"HOME": str(tmp_path)})
        with build_client(settings, fake_gh, ttl_s=ttl_s) as bundle:
            yield bundle

    return factory


def test_health_reports_version_and_migrations(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert body["dry_run"] is True
    assert body["local_only"] is True
    assert body["database"]["migrations"] == ["0001_init"]
    assert body["allowlisted_repos"] == 0


def test_github_status_is_read_only_and_audited(api: Callable[..., object]) -> None:
    with api() as (client, _db, audit):
        response = client.get("/api/github/status")
        entries = audit.list_entries()

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["login"] == "fake-user"
    assert body["account"]["scopes"] == ["gist", "read:org", "repo", "workflow"]
    assert body["rate_limit"]["remaining"] == 4990
    assert body["read_only"] is True

    actions = [entry.action for entry in entries]
    assert "start" in actions
    assert "auth_status" in actions


def test_github_repos_listing(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        response = client.get("/api/github/repos")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["read_only"] is True
    assert [repo["slug"] for repo in body["repos"]] == ["fake-user/alpha", "fake-user/beta"]
    assert body["repos"][1]["is_fork"] is True


def test_github_repos_limit_and_refresh(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        response = client.get("/api/github/repos", params={"limit": 1, "refresh": "true"})

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_allowlist_is_empty_by_default(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        response = client.get("/api/repos")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["repos"] == []


def test_unauthenticated_github_returns_401(api: Callable[..., object]) -> None:
    with api("unauthenticated") as (client, _db, _audit):
        response = client.get("/api/github/status")

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "gh_not_authenticated"
    assert "gh auth login" in error["message"]


def test_upstream_error_response_is_redacted(api: Callable[..., object]) -> None:
    with api("leaky_error") as (client, _db, _audit):
        response = client.get("/api/github/repos")

    assert response.status_code == 502
    leak = "gh" + "p_" + ("S3cr3t" * 6)
    assert leak not in response.text
    assert "[REDACTED:github_token]" in response.text


def test_invalid_limit_is_rejected(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        response = client.get("/api/github/repos", params={"limit": 0})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_audit_endpoint_pagination_and_filter(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        client.get("/api/github/repos")
        filtered = client.get("/api/audit", params={"event_type": "github.read"})
        bounded = client.get("/api/audit", params={"limit": 1})

    assert filtered.status_code == 200
    assert all(entry["event_type"] == "github.read" for entry in filtered.json()["entries"])
    assert bounded.status_code == 200
    assert bounded.json()["count"] == 1


def test_audit_limit_above_maximum_is_rejected(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        response = client.get("/api/audit", params={"limit": 501})

    assert response.status_code == 422


def test_unexpected_errors_do_not_leak_internals(api: Callable[..., object]) -> None:
    with api() as (client, _db, _audit):
        client.app.state.gitbot.github.status = lambda: (_ for _ in ()).throw(  # type: ignore[union-attr]
            RuntimeError("boom /home/secret/path")
        )
        response = client.get("/api/github/status")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "boom" not in response.text
    assert "/home/secret/path" not in response.text


def test_cli_check_mode_bootstraps_a_fresh_home(tmp_path: Path) -> None:
    """`gitbot --check` must validate config, migrate an empty DB, and exit 0."""
    env = {**os.environ, "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    env.pop("GITBOT_HOST", None)
    env.pop("GITBOT_PORT", None)

    result = subprocess.run(
        [sys.executable, "-m", "gitbot.main", "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ok: config valid" in result.stdout
    assert "bind=127.0.0.1:8765" in result.stdout
    assert (tmp_path / ".local" / "share" / "gitbot" / "gitbot.db").is_file()


def test_cli_refuses_non_loopback_host(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path), "GITBOT_HOST": "0.0.0.0"}

    result = subprocess.run(
        [sys.executable, "-m", "gitbot.main", "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert "non-loopback" in result.stderr
