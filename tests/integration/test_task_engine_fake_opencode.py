"""Integration tests: task engine with fake opencode in disposable temp repos."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import InvalidStateTransition, PolicyViolation
from gitbot.models import TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig
from gitbot.repos import RepoStore
from gitbot.tasks.engine import TaskEngine, recover_stale_tasks


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _build(
    tmp_path: Path,
    write_fake_opencode: Callable[..., Path],
    *,
    dry_run: bool = False,
    allowed_roots: tuple[Path, ...] | None = None,
    existing_config: str | None = None,
    sandbox_bwrap: str = "bwrap",
    timeout_s: float = 10,
) -> tuple[TaskEngine, Path, int]:
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    if existing_config is not None:
        (repo_dir / "opencode.json").write_text(existing_config, encoding="utf-8")
        subprocess.run(["git", "add", "opencode.json"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo_dir, check=True)
    repo = RepoStore(db).add(path=str(repo_dir), slug="local/repo", allow_commit=True)
    fake = write_fake_opencode()
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=timeout_s))
    engine = TaskEngine(
        db,
        audit,
        opencode=adapter,
        dry_run=dry_run,
        allowed_roots=allowed_roots if allowed_roots is not None else (tmp_path,),
        sandbox_bwrap=sandbox_bwrap,
    )
    return engine, repo_dir, repo.id


def test_task_engine_end_to_end_fake(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    prompt = (
        'write docs GITBOT_FAKE:{"text": "finished the analysis", '
        '"writes": {"docs/guide.md": "the analysis\\n"}}]]'
    )
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)
    task = engine.run_task(task_id)

    assert task["state"] == "awaiting_review"
    assert task["branch_name"] == "main"
    assert task["sandboxed"] == 1
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert task["snapshot_head"] == head
    assert task["diff_hash"] is not None
    assert task["output"] == "finished the analysis"
    assert task["output_truncated"] == 0
    assert task["approval_id"]

    # The temporary policy file never pollutes the reviewed diff, and the
    # source repository is untouched: no branch, config, or worktree change.
    assert not (repo_dir / "opencode.json").exists()
    assert "docs/guide.md" in task["diff_patch"]
    assert "the analysis" in task["diff_patch"]
    assert task["diff_files"] == '["docs/guide.md"]'
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""
    branches = subprocess.run(
        ["git", "branch", "--list", "gitbot/*"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() == ""
    approvals = engine.approvals.get(task["approval_id"])
    assert approvals.action.value == "commit"
    assert approvals.diff_hash == task["diff_hash"]


def test_task_engine_restores_existing_opencode_config(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    original = '{\n  "$schema": "https://opencode.ai/config.json"\n}\n'
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode, existing_config=original)
    prompt = 'write docs GITBOT_FAKE:{"write": "notes.txt::new\\n"}]]'
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)
    task = engine.run_task(task_id)

    assert task["state"] == "awaiting_review"
    assert (repo_dir / "opencode.json").read_text(encoding="utf-8") == original
    assert "notes.txt" in task["diff_patch"]
    # The policy document itself must never appear in the reviewed change.
    assert "opencode.json" not in task["diff_patch"]


def test_write_task_without_a_change_never_creates_an_approval(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    prompt = 'write docs GITBOT_FAKE:{"text": "I did not change anything"}]]'
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)

    with pytest.raises(PolicyViolation, match="no repository change"):
        engine.run_task(task_id)

    task = engine.get_task(task_id)
    assert task["state"] == "failed"
    assert task["approval_id"] is None
    assert task["diff_hash"] is None
    assert task["diff_patch"] is None
    assert "no repository change" in task["error_message"]
    with engine.db.connect() as conn:
        approvals = conn.execute(
            "SELECT COUNT(*) AS n FROM approvals WHERE task_id = ?", (task_id,)
        ).fetchone()
    assert approvals["n"] == 0
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""


def test_task_engine_refuses_dry_run(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode, dry_run=True)
    task_id = engine.create_task(repo_id, TaskType.DOCS, "write docs")

    with pytest.raises(PolicyViolation, match="dry-run"):
        engine.run_task(task_id)

    # The task stays queued so the user can disable dry-run and run it later.
    assert engine.get_task(task_id)["state"] == "queued"
    assert not (repo_dir / "opencode.json").exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "gitbot/*"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() == ""


def test_recover_stale_tasks_fails_running_rows_and_leaves_queued_rows(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, _repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    queued = engine.create_task(repo_id, TaskType.ANALYZE, "queued")
    stale = engine.create_task(repo_id, TaskType.ANALYZE, "stale")
    with engine.db.connect() as conn:
        conn.execute(
            "UPDATE tasks SET state = 'running', started_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (stale,),
        )

    assert recover_stale_tasks(engine.db) == [stale]
    stale_task = engine.get_task(stale)
    assert stale_task is not None
    assert stale_task["state"] == "failed"
    assert stale_task["finished_at"]
    assert "server restarted" in stale_task["error_message"]
    assert engine.get_task(queued)["state"] == "queued"

    with engine.db.connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id", (stale,)
        ).fetchall()
    assert "task.recovered.stale" in [row["event_type"] for row in events]
    assert recover_stale_tasks(engine.db) == []


def test_duplicate_run_is_rejected_while_first_run_is_active(
    tmp_path: Path,
    write_fake_opencode: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "analyze")
    entered = threading.Event()
    release = threading.Event()

    def blocked_impl(task: str) -> dict:
        assert task == task_id
        entered.set()
        assert release.wait(timeout=5)
        return {"id": task_id, "state": "blocked-test"}

    monkeypatch.setattr(engine, "_run_task_impl", blocked_impl)
    result: dict = {}

    def first_run() -> None:
        result.update(engine.run_task(task_id))

    worker = threading.Thread(target=first_run)
    worker.start()
    assert entered.wait(timeout=5)
    with pytest.raises(InvalidStateTransition, match="already running"):
        engine.run_task(task_id)
    release.set()
    worker.join(timeout=5)
    assert result["state"] == "blocked-test"

    # The reservation must be released even after a duplicate attempt.
    monkeypatch.setattr(engine, "_run_task_impl", lambda task: {"id": task, "state": "second"})
    assert engine.run_task(task_id)["state"] == "second"


def test_task_engine_fails_closed_outside_allowed_roots(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode, allowed_roots=(elsewhere,))
    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "analyze")

    with pytest.raises(PolicyViolation):
        engine.run_task(task_id)

    failed = engine.get_task(task_id)
    assert failed["state"] == "failed"
    assert "repository validation failed" in failed["error_message"]
    # No fallback: the repository is untouched and no task branch was created.
    branches = subprocess.run(
        ["git", "branch", "--list", "gitbot/*"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() == ""


def test_write_task_timeout_fails_and_leaves_repository_untouched(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode, timeout_s=0.2)
    prompt = 'sleep GITBOT_FAKE:{"sleep": 30}]]'
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)

    with pytest.raises(PolicyViolation, match="timed out"):
        engine.run_task(task_id)

    failed = engine.get_task(task_id)
    assert failed["state"] == "failed"
    assert "timed out" in failed["error_message"]
    assert failed["finished_at"]
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""


def test_task_engine_cancel_queued(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, _repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "analyze")

    cancelled = engine.cancel_task(task_id)
    assert cancelled["state"] == "cancelled"
    assert cancelled["finished_at"] is not None


def test_write_task_fails_closed_when_bwrap_is_missing(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    missing = tmp_path / "missing-bwrap"
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode, sandbox_bwrap=str(missing))
    prompt = 'write notes GITBOT_FAKE:{"write": "notes.txt::new\\n"}]]'
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)

    with pytest.raises(PolicyViolation, match="sandbox is unavailable"):
        engine.run_task(task_id)

    failed = engine.get_task(task_id)
    assert failed["state"] == "failed"
    assert "sandboxed OpenCode" in failed["error_message"]
    assert not (repo_dir / "notes.txt").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""


def test_write_task_never_uses_the_live_adapter(
    tmp_path: Path,
    write_fake_opencode: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("live OpenCodeAdapter.run must not be called")

    monkeypatch.setattr(OpenCodeAdapter, "run", _forbidden)
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    prompt = 'write notes GITBOT_FAKE:{"write": "notes.txt::new\\n"}]]'
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)
    task = engine.run_task(task_id)

    assert task["state"] == "awaiting_review"
    assert task["sandboxed"] == 1
    assert "notes.txt" in task["diff_patch"]
    assert not (repo_dir / "notes.txt").exists()


def test_write_task_cancel_terminates_the_sandbox_process(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    engine, repo_dir, repo_id = _build(tmp_path, write_fake_opencode)
    prompt = 'sleep GITBOT_FAKE:{"sleep": 30}]]'
    task_id = engine.create_task(repo_id, TaskType.DOCS, prompt)
    errors: list[Exception] = []

    def run() -> None:
        try:
            engine.run_task(task_id)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = engine.get_task(task_id)
        if current and current["state"] == "running":
            break
        time.sleep(0.02)
    else:
        pytest.fail("task did not enter the running state")
    time.sleep(0.25)

    requested = engine.cancel_task(task_id)
    assert requested["state"] == "running"
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []
    final = engine.get_task(task_id)
    assert final["state"] == "cancelled"
    assert final["finished_at"]
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""
