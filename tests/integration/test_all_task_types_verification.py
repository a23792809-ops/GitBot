"""Verification matrix for all seven task types (fake model runner, isolated).

Covers: create, allowlist/permissions, execution in correct lane, sandbox/tool
context, stored results, safe failure/invalid-output/provider handling,
cancellation, approval requirements for write tasks, and no unauthorized
commit/push. Uses disposable repositories/databases only.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from gitbot.audit import AuditLog
from gitbot.config import load_settings
from gitbot.db import Database
from gitbot.errors import InvalidStateTransition, PolicyViolation
from gitbot.models import READ_ONLY_TASK_TYPES, ApprovalAction, TaskState, TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig
from gitbot.opencode.gateway import classify_provider_error
from gitbot.repos import RepoStore
from gitbot.security.sandbox import detect_sandbox_capabilities
from gitbot.tasks.approvals import compute_diff_hash
from gitbot.tasks.engine import TaskEngine

ALL = list(TaskType)
WRITE_TYPES = [t for t in TaskType if t not in READ_ONLY_TASK_TYPES]
READ_TYPES = list(READ_ONLY_TASK_TYPES)


def _sandbox_ready() -> None:
    caps = detect_sandbox_capabilities()
    if not caps.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {caps.detail}")


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=path, check=check, capture_output=True, text=True)


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "GitBot Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    (path / "app.py").write_text("print('one')\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _mk_engine(
    tmp_path: Path,
    write_fake_opencode: Callable[..., Path],
    *,
    dry_run: bool = False,
    timeout_s: float = 20.0,
    allowed_task_types: str = "analyze,explain,bugfix,feature,docs,deps,tests",
    allow_commit: bool = True,
    fake_name: str = "opencode",
) -> tuple[TaskEngine, Path, int, str]:
    _sandbox_ready()
    db = Database(tmp_path / "gitbot.db")
    db.migrate()
    audit = AuditLog(db)
    repo_dir = tmp_path / "repo"
    head = _init_repo(repo_dir)
    repo = RepoStore(db).add(
        path=str(repo_dir),
        slug="local/repo",
        allow_commit=allow_commit,
        allowed_task_types=[TaskType(t) for t in allowed_task_types.split(",")],
    )
    fake = write_fake_opencode(fake_name)
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=timeout_s))
    engine = TaskEngine(
        db,
        audit,
        opencode=adapter,
        dry_run=dry_run,
        allowed_roots=(tmp_path,),
    )
    return engine, repo_dir, repo.id, head


def _assert_repo_untouched(repo_dir: Path, head: str) -> None:
    assert _git(repo_dir, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(repo_dir, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo_dir, "branch", "--list", "gitbot/*").stdout.strip() == ""


@pytest.mark.parametrize("task_type", ALL, ids=lambda t: t.value)
def test_create_and_store_all_types(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(tmp_path, write_fake_opencode)
    task_id = engine.create_task(repo_id, task_type, f"prompt for {task_type.value}")
    task = engine.get_task(task_id)
    assert task is not None
    assert task["id"] == task_id
    assert task["repo_id"] == repo_id
    assert task["task_type"] == task_type.value
    assert task["state"] == TaskState.QUEUED.value
    assert task["prompt"] == f"prompt for {task_type.value}"
    _assert_repo_untouched(repo_dir, head)


@pytest.mark.parametrize("task_type", ALL, ids=lambda t: t.value)
def test_allowlist_enforcement_rejects_disabled_type(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, _repo_dir, repo_id, _head = _mk_engine(
        tmp_path, write_fake_opencode, allowed_task_types="analyze"
    )
    if task_type is TaskType.ANALYZE:
        tid = engine.create_task(repo_id, task_type, "ok")
        assert engine.get_task(tid)["state"] == TaskState.QUEUED.value
        return
    with pytest.raises(PolicyViolation, match="disabled for this repository"):
        engine.create_task(repo_id, task_type, "nope")


@pytest.mark.parametrize("task_type", READ_TYPES, ids=lambda t: t.value)
def test_read_only_executes_in_sandbox_and_stores_result(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(tmp_path, write_fake_opencode, dry_run=True)
    directives = {
        "text": f"{task_type.value}-ok",
        "probes": ["/repo/.git", "/repo/README.md"],
    }
    prompt = f"summarize GITBOT_FAKE:{json.dumps(directives)}]]"
    task_id = engine.create_task(repo_id, task_type, prompt)
    task = engine.run_task(task_id)

    terminal = TaskState.EXPLAINED if task_type is TaskType.EXPLAIN else TaskState.ANALYZED
    assert task["state"] == terminal.value
    assert task["sandboxed"] == 1
    assert task["snapshot_head"] == head
    assert task["diff_patch"] is None
    assert task["diff_hash"] is None
    assert task["approval_id"] is None
    visibility = json.loads(task["output"] or "{}")
    assert visibility["/repo/.git"] is False
    assert visibility["/repo/README.md"] is True
    _assert_repo_untouched(repo_dir, head)
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize("task_type", WRITE_TYPES, ids=lambda t: t.value)
def test_write_executes_in_disposable_sandbox_and_stores_review(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(tmp_path, write_fake_opencode, dry_run=False)
    fname = f"{task_type.value}.txt"
    directives = {
        "text": f"{task_type.value} done",
        "writes": {fname: "new\n"},
        "probes": ["/repo/.git", "/work/repo/.git", "/repo/README.md"],
    }
    prompt = f"change GITBOT_FAKE:{json.dumps(directives)}]]"
    task_id = engine.create_task(repo_id, task_type, prompt)
    task = engine.run_task(task_id)

    assert task["state"] == TaskState.AWAITING_REVIEW.value
    assert task["sandboxed"] == 1
    assert task["snapshot_head"] == head
    assert task["diff_hash"] == compute_diff_hash(task["diff_patch"] or "")
    assert fname in (task["diff_patch"] or "")
    assert task["approval_id"]
    visibility = json.loads(task["output"] or "{}")
    assert visibility["/repo/.git"] is False
    assert visibility["/work/repo/.git"] is False
    assert visibility["/repo/README.md"] is True
    assert not (repo_dir / fname).exists()
    _assert_repo_untouched(repo_dir, head)
    approval = engine.approvals.get(task["approval_id"])
    assert approval.action is ApprovalAction.COMMIT
    assert approval.diff_hash == task["diff_hash"]


@pytest.mark.parametrize("task_type", WRITE_TYPES, ids=lambda t: t.value)
def test_write_task_blocked_in_dry_run_no_unauthorized_change(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(tmp_path, write_fake_opencode, dry_run=True)
    fname = f"{task_type.value}-dry.txt"
    prompt = f"change GITBOT_FAKE:{json.dumps({'writes': {fname: 'x\n'}})}]]"
    task_id = engine.create_task(repo_id, task_type, prompt)
    with pytest.raises(PolicyViolation, match="dry-run"):
        engine.run_task(task_id)
    task = engine.get_task(task_id)
    assert task["state"] == TaskState.QUEUED.value
    assert not (repo_dir / fname).exists()
    _assert_repo_untouched(repo_dir, head)
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize("task_type", READ_TYPES, ids=lambda t: t.value)
def test_read_only_failure_and_cancellation_handled_safely(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(
        tmp_path, write_fake_opencode, dry_run=True, timeout_s=20.0
    )
    prompt = f"slow GITBOT_FAKE:{json.dumps({'sleep': 30})}]]"
    task_id = engine.create_task(repo_id, task_type, prompt)
    errors: list[Exception] = []

    def run() -> None:
        try:
            engine.run_task(task_id)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.5)
    engine.cancel_task(task_id)
    worker.join(timeout=15)
    task = engine.get_task(task_id)
    assert task["state"] in (TaskState.CANCELLED.value, TaskState.FAILED.value)
    assert task.get("diff_patch") is None
    assert task.get("approval_id") is None
    _assert_repo_untouched(repo_dir, head)


@pytest.mark.parametrize("task_type", WRITE_TYPES, ids=lambda t: t.value)
def test_write_failure_cancellation_and_no_unauthorized_commit(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(tmp_path, write_fake_opencode, dry_run=False)
    prompt = f"slow GITBOT_FAKE:{json.dumps({'sleep': 30})}]]"
    task_id = engine.create_task(repo_id, task_type, prompt)
    engine.opencode.config = OpenCodeConfig(
        executable=engine.opencode.config.executable, timeout_s=0.2
    )
    with pytest.raises(PolicyViolation):
        engine.run_task(task_id)
    task = engine.get_task(task_id)
    assert task["state"] == TaskState.FAILED.value
    _assert_repo_untouched(repo_dir, head)
    with pytest.raises(InvalidStateTransition):
        engine.commit_task(task_id)


@pytest.mark.parametrize("task_type", WRITE_TYPES, ids=lambda t: t.value)
def test_write_requires_approval_and_rejects_unauthorized_commit(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(
        tmp_path, write_fake_opencode, dry_run=False, allow_commit=True
    )
    fname = f"{task_type.value}-rev.txt"
    prompt = f"change GITBOT_FAKE:{json.dumps({'writes': {fname: 'x\n'}})}]]"
    task_id = engine.create_task(repo_id, task_type, prompt)
    task = engine.run_task(task_id)
    assert task["state"] == TaskState.AWAITING_REVIEW.value
    with pytest.raises(InvalidStateTransition):
        engine.commit_task(task_id)
    cancelled = engine.cancel_task(task_id)
    assert cancelled["state"] == TaskState.CANCELLED.value
    with pytest.raises(InvalidStateTransition):
        engine.commit_task(task_id)
    assert not (repo_dir / fname).exists()
    _assert_repo_untouched(repo_dir, head)


@pytest.mark.parametrize("task_type", WRITE_TYPES, ids=lambda t: t.value)
def test_no_unauthorized_push_path_exists(
    tmp_path: Path, write_fake_opencode: Callable[..., Path], task_type: TaskType
) -> None:
    engine, repo_dir, repo_id, head = _mk_engine(tmp_path, write_fake_opencode, dry_run=False)
    assert not hasattr(engine, "push_task")
    assert not hasattr(engine, "create_pull_request")
    task_id = engine.create_task(repo_id, task_type, "noop")
    _assert_repo_untouched(repo_dir, head)
    assert engine.get_task(task_id)["state"] == TaskState.QUEUED.value


def test_provider_error_classification_and_redaction_safety() -> None:
    for msg, code in [
        ("provider.no-route: model unavailable", "ai_gateway_model_unavailable"),
        ("Rate limit exceeded: free-models-per-day", "ai_gateway_rate_limited"),
        ("authentication failed: invalid key sk-SECRET123", "ai_gateway_auth_failed"),
    ]:
        exc = classify_provider_error(msg)
        assert exc is not None
        assert exc.code == code
        assert "sk-SECRET123" not in str(exc)
        assert "SECRET123" not in (exc.message or "")


def test_invalid_gateway_model_reference_rejected_config() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.raises(Exception, match="provider/model"):
        load_settings(env={"HOME": td, "GITBOT_AI_GATEWAY_MODEL": "not-a-model"})
