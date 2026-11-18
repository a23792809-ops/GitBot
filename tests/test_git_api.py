"""HTTP surface for read-only git inspection."""

from __future__ import annotations

import shutil
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
from gitbot.repos import RepoStore
from gitbot.security.tokens import ensure_token


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "GitBot Test")
    (path / "app.py").write_text("print('one')\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-q", "-m", "initial")
    return path


@contextmanager
def build(
    settings: Settings, fake_gh: Path, *, authenticated: bool = True
) -> Iterator[tuple[TestClient, Database]]:
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    app = create_app(
        settings=settings,
        database=database,
        github=GithubService(GhClient(str(fake_gh)), audit=audit),
        audit=audit,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        if authenticated:
            # Every git read endpoint is token-gated; make the default explicit.
            token = ensure_token(settings.token_file)
            client.headers.update({"Authorization": f"Bearer {token}"})
        yield client, database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env={"HOME": str(tmp_path)})


@pytest.fixture
def repo_path(settings: Settings) -> Path:
    return _init_repo(settings.home / "projects" / "sample")


def _seed(settings: Settings, repo_path: Path) -> int:
    database = Database(settings.db_path)
    database.migrate()
    repo = RepoStore(database).add(path=str(repo_path.resolve()), slug="local/sample")
    return repo.id


@pytest.mark.parametrize(
    ("suffix", "params"),
    [
        ("/git/status", None),
        ("/git/diff", None),
        ("/git/preflight", {"task_id": "x"}),
    ],
)
def test_git_reads_require_the_bearer_token(
    settings: Settings,
    fake_gh: Path,
    repo_path: Path,
    suffix: str,
    params: dict[str, str] | None,
) -> None:
    repo_id = _seed(settings, repo_path)
    with build(settings, fake_gh, authenticated=False) as (client, _db):
        response = client.get(f"/api/repos/{repo_id}{suffix}", params=params)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_git_status_endpoint(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    repo_id = _seed(settings, repo_path)
    with build(settings, fake_gh) as (client, _db):
        response = client.get(f"/api/repos/{repo_id}/git/status")

    assert response.status_code == 200
    body = response.json()
    assert body["repo"] == "local/sample"
    assert body["status"]["branch"] == "main"
    assert body["status"]["clean"] is True


def test_git_diff_endpoint_reflects_working_tree(
    settings: Settings, fake_gh: Path, repo_path: Path
) -> None:
    repo_id = _seed(settings, repo_path)
    (repo_path / "app.py").write_text("print('changed')\n", encoding="utf-8")

    with build(settings, fake_gh) as (client, _db):
        response = client.get(f"/api/repos/{repo_id}/git/diff")

    assert response.status_code == 200
    diff = response.json()["diff"]
    assert diff["files"] == ["app.py"]
    assert "print('changed')" in diff["patch"]
    assert diff["truncated"] is False


def test_git_diff_rejects_option_like_base_without_writing(
    settings: Settings, fake_gh: Path, repo_path: Path
) -> None:
    repo_id = _seed(settings, repo_path)
    target = settings.home / "must-not-be-written.txt"
    target.write_text("untouched", encoding="utf-8")

    with build(settings, fake_gh) as (client, _db):
        response = client.get(
            f"/api/repos/{repo_id}/git/diff",
            params={"base": f"--output={target}"},
        )

    assert response.status_code == 422
    assert target.read_text(encoding="utf-8") == "untouched"


def test_git_preflight_endpoint(settings: Settings, fake_gh: Path, repo_path: Path) -> None:
    repo_id = _seed(settings, repo_path)
    with build(settings, fake_gh) as (client, _db):
        response = client.get(f"/api/repos/{repo_id}/git/preflight", params={"task_id": "run-42"})

    assert response.status_code == 200
    preflight = response.json()["preflight"]
    assert preflight["ok"] is True
    assert preflight["suggested_branch"] == "gitbot/run-42"
    assert preflight["states_only"] == "no working tree changes were made"


def test_git_endpoints_404_for_unknown_repo(settings: Settings, fake_gh: Path) -> None:
    with build(settings, fake_gh) as (client, _db):
        response = client.get("/api/repos/999/git/status")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_git_endpoints_404_when_path_disappears(
    settings: Settings, fake_gh: Path, repo_path: Path
) -> None:
    repo_id = _seed(settings, repo_path)
    shutil.rmtree(repo_path)

    with build(settings, fake_gh) as (client, _db):
        response = client.get(f"/api/repos/{repo_id}/git/status")

    assert response.status_code == 404
    assert "no longer exists" in response.json()["error"]["message"]


def test_git_endpoints_are_read_only_for_the_working_tree(
    settings: Settings, fake_gh: Path, repo_path: Path
) -> None:
    repo_id = _seed(settings, repo_path)
    before = _git(repo_path, "rev-parse", "HEAD")
    (repo_path / "app.py").write_text("print('still dirty')\n", encoding="utf-8")

    with build(settings, fake_gh) as (client, _db):
        client.get(f"/api/repos/{repo_id}/git/status")
        client.get(f"/api/repos/{repo_id}/git/diff")
        client.get(f"/api/repos/{repo_id}/git/preflight", params={"task_id": "x"})

    after = _git(repo_path, "rev-parse", "HEAD")
    assert before == after
    assert (repo_path / "app.py").read_text(encoding="utf-8") == "print('still dirty')\n"
