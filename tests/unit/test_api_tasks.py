"""API tests for tasks, approvals, cancellation, and local commit."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import load_settings
from gitbot.db import Database
from gitbot.errors import InvalidStateTransition
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.gitops.repo import GitRepo
from gitbot.models import TaskState
from gitbot.repos import RepoStore
from gitbot.security.sandbox import detect_sandbox_capabilities
from gitbot.tasks.engine import TaskEngine, recover_stale_tasks

TOKEN = "x" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@contextmanager
def build_client(
    tmp_path: Path,
    fake_gh: Path,
    *,
    dry_run: bool = True,
    retry_delay_s: int | None = None,
) -> Iterator[tuple[TestClient, Database]]:
    env = {"HOME": str(tmp_path), "GITBOT_AI_GATEWAY_ENABLED": "false"}
    env["GITBOT_DRY_RUN"] = "true" if dry_run else "false"
    if retry_delay_s is not None:
        env["GITBOT_RETRY_DELAY_S"] = str(retry_delay_s)
    settings = load_settings(env=env)
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    client = GhClient(str(fake_gh), timeout_s=10)
    service = GithubService(client, audit=audit, ttl_s=60)
    app = create_app(settings=settings, database=database, github=service, audit=audit)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, database


def _write_token(tmp_path: Path) -> None:
    token_file = tmp_path / ".config" / "gitbot" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)


def _seed_repo(tmp_path: Path, db: Database, *, allow_commit: bool = True) -> int:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    return RepoStore(db).add(path=str(repo_dir), slug="local/repo", allow_commit=allow_commit).id


def _fake_prompt(prompt: str, **directives: str) -> str:
    """Attach hermetic fake-opencode directives that survive the sandbox env clear."""
    if not directives:
        return prompt
    return prompt + " GITBOT_FAKE:" + json.dumps(directives) + "]]"


def _fake_on_path(
    tmp_path: Path,
    monkeypatch,
    write_fake_opencode: Callable[..., Path],
) -> Path:
    fake = write_fake_opencode()
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    return fake


def test_create_task_invalid_input(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, _db):
        _write_token(tmp_path)
        # no token
        r = client.post("/api/tasks", json={"repo_id": 1, "prompt": "hi"})
        assert r.status_code == 401
        # invalid repo id type
        r = client.post("/api/tasks", json={"repo_id": "x", "prompt": "hi"}, headers=HEADERS)
        assert r.status_code == 422
        # bool is an int subclass, but not a repository id
        r = client.post("/api/tasks", json={"repo_id": True, "prompt": "hi"}, headers=HEADERS)
        assert r.status_code == 422
        # missing prompt
        r = client.post("/api/tasks", json={"repo_id": 1}, headers=HEADERS)
        assert r.status_code == 422
        # oversized prompt
        r = client.post(
            "/api/tasks",
            json={"repo_id": 1, "prompt": "x" * 32_001},
            headers=HEADERS,
        )
        assert r.status_code == 422
        # oversized model name
        r = client.post(
            "/api/tasks",
            json={"repo_id": 1, "prompt": "hi", "model": "m" * 201},
            headers=HEADERS,
        )
        assert r.status_code == 422
        # unknown repository
        r = client.post("/api/tasks", json={"repo_id": 999, "prompt": "hi"}, headers=HEADERS)
        assert r.status_code == 422


def test_list_and_get_task(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        r = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "analyze", "prompt": "do it"},
            headers=HEADERS,
        )
        assert r.status_code == 200
        task_id = r.json()["task"]["id"]
        assert client.get("/api/tasks", headers=HEADERS).status_code == 200
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS)
        assert detail.status_code == 200
        assert detail.json()["diff"] is None
        assert client.get("/api/tasks/nonexistent", headers=HEADERS).status_code == 404


def test_task_list_omits_large_fields_and_truncates_prompt(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch,
    write_fake_opencode: Callable[..., Path],
) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_TEXT", "X" * 5000)
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        created = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt(
                    "p" * 900,
                    text="X" * 5000,
                    writes={"docs/list.md": "listed\n"},
                ),
            },
            headers=HEADERS,
        ).json()["task"]
        client.post(f"/api/tasks/{created['id']}/run", headers=HEADERS)

        listed = client.get("/api/tasks", headers=HEADERS).json()["tasks"][0]
        assert "output" not in listed
        assert "diff_patch" not in listed
        assert len(listed["prompt"]) == 200

        detail = client.get(f"/api/tasks/{created['id']}", headers=HEADERS).json()
        assert detail["task"]["output"] == "X" * 5000
        assert detail["diff"] is not None


def test_run_task_requires_dry_run_disabled(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=True) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "docs", "prompt": "do it"},
            headers=HEADERS,
        ).json()["task"]["id"]

        r = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert r.status_code == 403
        assert "dry-run" in r.json()["error"]["message"]
        assert (
            client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]["state"] == "queued"
        )


def test_run_task_against_allowlisted_repo(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do it", writes={"docs/run.md": "ran\n"}),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        r = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert r.status_code == 200
        task = r.json()["task"]
        assert task["state"] == "awaiting_review"
        assert task["output"] == "done"
        assert task["approval_id"]
        assert "docs/run.md" in task["diff_patch"]


def test_analyze_task_runs_read_only_in_dry_run(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch,
    write_fake_opencode: Callable[..., Path],
) -> None:
    capabilities = detect_sandbox_capabilities()
    if not capabilities.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {capabilities.detail}")
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=True) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "analyze", "prompt": "look around"},
            headers=HEADERS,
        ).json()["task"]["id"]

        r = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert r.status_code == 200, r.json()
        task = r.json()["task"]
        assert task["state"] == "analyzed"
        assert task["sandboxed"] == 1
        assert task["snapshot_head"]
        assert task["output"] == "done"
        assert task["approval_id"] is None
        assert task["diff_hash"] is None

        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["diff"] is None
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0

        repo_dir = tmp_path / "repo"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        branches = subprocess.run(
            ["git", "branch", "--list", "gitbot/*"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status.strip() == ""
        assert branches.strip() == ""


def test_create_task_with_schedule_and_retry_budget(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        response = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": "later",
                "scheduled_at": "2030-01-01T12:00:00Z",
                "max_retries": 2,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        task = response.json()["task"]
        assert task["scheduled_at"] == "2030-01-01T12:00:00Z"
        assert task["max_retries"] == 2
        assert task["retry_count"] == 0

        status = client.get("/api/tasks/scheduler/status", headers=HEADERS).json()["scheduler"]
        assert status["next_scheduled_at"] == "2030-01-01T12:00:00Z"


def test_scheduler_tick_skips_change_tasks_in_dry_run(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": "later",
                "scheduled_at": "2000-01-01T00:00:00Z",
            },
            headers=HEADERS,
        ).json()["task"]["id"]

        response = client.post("/api/tasks/scheduler/tick", headers=HEADERS)
        assert response.status_code == 200, response.json()
        results = response.json()["scheduler"]["results"]
        assert results == [
            {
                "task_id": task_id,
                "status": "skipped",
                "reason": "dry_run",
                "scheduled_at_cleared": True,
            }
        ]
        assert (
            client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]["state"] == "queued"
        )


def test_retry_uses_the_persisted_retry_budget(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": "try",
                "max_retries": 1,
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        with db.connect() as conn:
            conn.execute("UPDATE tasks SET state = 'failed' WHERE id = ?", (task_id,))

        retried = client.post(f"/api/tasks/{task_id}/retry", headers=HEADERS)
        assert retried.status_code == 200, retried.json()
        child = retried.json()["task"]
        assert child["state"] == "queued"
        assert child["parent_task_id"] == task_id
        assert child["retry_count"] == 1

        duplicate_parent = client.post(f"/api/tasks/{task_id}/retry", headers=HEADERS)
        assert duplicate_parent.status_code == 403
        assert "already exists" in duplicate_parent.json()["error"]["message"]

        with db.connect() as conn:
            conn.execute("UPDATE tasks SET state = 'failed' WHERE id = ?", (child["id"],))
        exhausted = client.post(f"/api/tasks/{child['id']}/retry", headers=HEADERS)
        assert exhausted.status_code == 403
        assert "budget exhausted" in exhausted.json()["error"]["message"]


def test_repository_task_type_policy_is_enforced(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_git_repo(repo_dir)
        repo_id = (
            RepoStore(db)
            .add(
                path=str(repo_dir),
                slug="local/repo",
                allowed_task_types=["docs"],
            )
            .id
        )

        denied = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "analyze", "prompt": "look"},
            headers=HEADERS,
        )
        allowed = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "docs", "prompt": "write"},
            headers=HEADERS,
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "policy_violation"
    assert "disabled" in denied.json()["error"]["message"]
    assert allowed.status_code == 200


def test_task_type_policy_is_rechecked_before_execution(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "docs", "prompt": "write docs"},
            headers=HEADERS,
        ).json()["task"]["id"]

        RepoStore(db).update(repo_id, allowed_task_types=["analyze"])
        denied = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)

        assert denied.status_code == 403
        assert "no longer enabled" in denied.json()["error"]["message"]
        task = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]
        assert task["state"] == "failed"
        assert "no longer enabled" in task["error_message"]


def test_explain_task_is_read_only(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch,
    write_fake_opencode: Callable[..., Path],
) -> None:
    capabilities = detect_sandbox_capabilities()
    if not capabilities.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {capabilities.detail}")
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "explain", "prompt": "explain"},
            headers=HEADERS,
        ).json()["task"]["id"]

        response = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert response.status_code == 200, response.json()
        task = response.json()["task"]
        assert task["state"] == "explained"
        assert task["sandboxed"] == 1
        assert task["approval_id"] is None
        events = [
            event["event_type"]
            for event in client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["events"]
        ]
        assert "task.explained" in events


def test_approve_requires_matching_live_diff_and_commit(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "feature",
                "prompt": _fake_prompt("add", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert task["state"] == "awaiting_review"
        assert task["diff_hash"]
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert "notes.txt" in detail["diff"]["patch"]
        assert detail["diff"]["stored"] is True

        # A client-chosen hash that does not match the live tree is refused.
        wrong = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": "0" * 64},
            headers=HEADERS,
        )
        assert wrong.status_code == 403

        # The real hash comes from the server-side diff, not from the client.
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200
        assert (
            client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]["state"]
            == "approved"
        )

        committed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert committed.status_code == 200, committed.json()
        assert committed.json()["task"]["state"] == "committed"
        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert log.startswith("gitbot: feature task")


def test_commit_refuses_when_sandboxed_tests_failed(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        RepoStore(db).update(
            repo_id,
            test_command='python3 -c "import sys; sys.exit(3)"',
        )
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert task["state"] == "awaiting_review"
        assert task["tests_status"] == "failed"
        assert task["test_exit_code"] == 3

        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 409
        assert "tests did not pass" in approved.json()["detail"]["error"]["message"]
        committed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert committed.status_code == 409
        assert "cannot commit task in state" in committed.json()["error"]["message"]


def test_commit_requires_explicit_permission(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db, allow_commit=False)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]

        client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        r = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert r.status_code == 403
        assert "not enabled" in r.json()["error"]["message"]


def test_commit_refuses_changed_working_tree(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )

        # Change the tree after review: the approval must no longer apply.
        (tmp_path / "repo" / "notes.txt").write_text("tampered\n", encoding="utf-8")
        r = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert r.status_code == 403
        assert "changed since review" in r.json()["error"]["message"]


def test_cancel_queued_task(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        r = client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["task"]["state"] == "cancelled"


def test_approve_refuses_changed_working_tree(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert task["state"] == "awaiting_review"

        # A diff that no longer matches the reviewed hash must never be approved.
        (tmp_path / "repo" / "notes.txt").write_text("tampered\n", encoding="utf-8")
        r = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert r.status_code == 403
        assert "changed since" in r.json()["detail"]["error"]["message"]
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "awaiting_review"


def test_cancel_awaiting_review_revokes_the_approval(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert task["approval_id"]

        cancelled = client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["task"]["state"] == "cancelled"
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0

        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 409


def test_reject_cancels_the_task(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]

        rejected = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/reject", headers=HEADERS
        )
        assert rejected.status_code == 200
        assert (
            client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]["state"]
            == "cancelled"
        )
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0


def test_reject_after_approval_is_refused_and_does_not_consume_the_approval(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200

        rejected = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/reject", headers=HEADERS
        )
        assert rejected.status_code == 409
        assert (
            client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]["state"]
            == "approved"
        )

        committed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert committed.status_code == 200, committed.json()
        assert committed.json()["task"]["state"] == "committed"


def test_cancel_approved_task_revokes_approval_and_blocks_commit(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200

        cancelled = client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
        assert cancelled.status_code == 200, cancelled.json()
        assert cancelled.json()["task"]["state"] == "cancelled"

        committed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert committed.status_code == 409
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0

        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        event_types = [event["event_type"] for event in detail["events"]]
        assert "task.approval_revoked" in event_types
        assert "task.state.cancelled" in event_types


def test_pending_approvals_exclude_expired_rows(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 1

        with db.connect() as conn:
            conn.execute(
                "UPDATE approvals SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (task["approval_id"],),
            )
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0


def test_approvals_pending_and_errors(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, _db):
        _write_token(tmp_path)
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0
        r = client.post(
            "/api/tasks/approvals/nonexist/approve",
            json={"diff_hash": "x"},
            headers=HEADERS,
        )
        assert r.status_code in (404, 403)
        r = client.post("/api/tasks/approvals/nonexist/reject", headers=HEADERS)
        assert r.status_code in (404, 403)


def test_retry_schedules_a_delayed_task(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh, retry_delay_s=120) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": "retry later",
                "max_retries": 1,
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        with db.connect() as conn:
            conn.execute("UPDATE tasks SET state = 'failed' WHERE id = ?", (task_id,))

        retried = client.post(f"/api/tasks/{task_id}/retry", headers=HEADERS)
        assert retried.status_code == 200, retried.json()
        child = retried.json()["task"]
        assert child["state"] == "queued"
        assert child["scheduled_at"]
        assert child["scheduled_at"] > "2026-01-01T00:00:00Z"
        status = client.get("/api/tasks/scheduler/status", headers=HEADERS).json()["scheduler"]
        assert status["next_scheduled_at"] == child["scheduled_at"]


def test_stale_state_update_is_rejected(tmp_path: Path, fake_gh: Path) -> None:
    with build_client(tmp_path, fake_gh) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={"repo_id": repo_id, "task_type": "docs", "prompt": "stale"},
            headers=HEADERS,
        ).json()["task"]["id"]
        with db.connect() as conn:
            conn.execute("UPDATE tasks SET state = 'cancelled' WHERE id = ?", (task_id,))

        engine = TaskEngine(db, AuditLog(db))
        with pytest.raises(InvalidStateTransition, match="concurrently"):
            engine._update_state(task_id, TaskState.QUEUED, TaskState.RUNNING)


def test_commit_failure_rolls_back_the_applied_patch(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200

        original_run = GitRepo.run

        def fail_commit(self: GitRepo, args, **kwargs):
            if args and args[0] == "commit":
                raise RuntimeError("simulated commit failure")
            return original_run(self, args, **kwargs)

        monkeypatch.setattr(GitRepo, "run", fail_commit)
        failed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert failed.status_code == 500

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status.strip() == ""
        assert not (tmp_path / "repo" / "notes.txt").exists()
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "failed"
        assert {event["event_type"] for event in detail["events"]} >= {
            "task.commit_started",
            "task.commit_failed",
        }
        with db.connect() as conn:
            approval = conn.execute(
                "SELECT used_at FROM approvals WHERE id = ?", (task["approval_id"],)
            ).fetchone()
        assert approval["used_at"], "a failed commit must not leave a replayable approval"
        assert client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS).status_code == 409


def test_startup_fails_an_interrupted_commit_and_revokes_the_approval(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    """A crash after ``task.commit_started`` must leave no stale, reusable approval."""
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200

        # Simulate process death after the durable commit journal entry.
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, '2026-01-01T00:00:00Z', 'task.commit_started', 'system', '{}')",
                (task_id,),
            )
        recovered = recover_stale_tasks(db)
        assert task_id in recovered

        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "failed"
        assert "local commit" in detail["task"]["error_message"]
        assert "task.recovered.interrupted_commit" in {
            event["event_type"] for event in detail["events"]
        }
        with db.connect() as conn:
            approval = conn.execute(
                "SELECT used_at FROM approvals WHERE id = ?", (task["approval_id"],)
            ).fetchone()
        assert approval["used_at"]

        blocked = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert blocked.status_code == 409
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status.strip() == ""
        assert not (tmp_path / "repo" / "notes.txt").exists()


def test_commit_with_expired_approval_is_refused_without_touching_the_repo(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200
        # Approval expires after the human clicked approve.
        with db.connect() as conn:
            conn.execute(
                "UPDATE approvals SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (task["approval_id"],),
            )
        r = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "approval_expired"
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "approved"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status.strip() == ""
        with db.connect() as conn:
            approval = conn.execute(
                "SELECT used_at FROM approvals WHERE id = ?", (task["approval_id"],)
            ).fetchone()
        assert approval["used_at"] is None, "an expired approval must not be consumed"


def test_commit_refuses_when_head_moved_after_approval(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200
        # Someone (or another local tool) moves HEAD after the approval.
        repo_dir = tmp_path / "repo"
        (repo_dir / "other.txt").write_text("independent work\n", encoding="utf-8")
        subprocess.run(["git", "add", "other.txt"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "user work"], cwd=repo_dir, check=True)
        r = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert r.status_code == 403
        assert "HEAD changed" in r.json()["error"]["message"]
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "approved"


def test_commit_refuses_when_stored_patch_was_tampered(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200
        with db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET diff_patch = ? WHERE id = ?",
                ("garbage patch\n", task_id),
            )
        r = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert r.status_code == 403
        assert "stored review diff changed" in r.json()["error"]["message"]
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "approved"


def test_write_task_with_git_metadata_patch_fails_before_review(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write=".gitattributes::* -diff\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        run = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert run.status_code == 403, run.json()
        assert "Git metadata" in run.json()["error"]["message"]
        task = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]
        assert task["state"] == "failed"
        assert "Git metadata" in (task["error_message"] or "")
        assert task["approval_id"] is None


def test_approve_refuses_a_patch_writing_git_metadata(
    tmp_path: Path, fake_gh: Path, monkeypatch, write_fake_opencode: Callable[..., Path]
) -> None:
    _fake_on_path(tmp_path, monkeypatch, write_fake_opencode)
    with build_client(tmp_path, fake_gh, dry_run=False) as (client, db):
        _write_token(tmp_path)
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "docs",
                "prompt": _fake_prompt("do", write="notes.txt::new work\n"),
            },
            headers=HEADERS,
        ).json()["task"]["id"]
        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert task["state"] == "awaiting_review"
        # Tamper with the stored patch so it touches .gitattributes; the approve
        # gate must refuse even before the hash check is consulted.
        with db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET diff_patch = ? WHERE id = ?",
                ("diff --git a/.gitattributes b/.gitattributes\n", task_id),
            )
        r = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert r.status_code == 409
        assert "Git metadata" in r.json()["detail"]["error"]["message"]
