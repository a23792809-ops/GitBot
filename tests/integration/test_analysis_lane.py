"""Integration tests for the read-only, sandboxed analysis lane.

These tests use the real bubblewrap sandbox and a fake ``opencode`` executable.
No network, model, provider credential, or repository write is involved.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import GitBotError
from gitbot.models import TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig, RunClassification
from gitbot.opencode.analysis import ANALYSIS_RULES, run_analysis
from gitbot.opencode.policy import PermissionRule
from gitbot.repos import RepoStore
from gitbot.security.sandbox import detect_sandbox_capabilities
from gitbot.tasks.engine import TaskEngine

_DEBUG_CONFIG = """
path = os.path.join(os.getcwd(), "opencode.json")
with open(path, encoding="utf-8") as handle:
    info = json.load(handle)
print(json.dumps([
    {"type": "directory", "path": "/work/config/opencode"},
    {"type": "document", "path": path, "info": info},
]))
sys.exit(0)
"""

_SUCCESS = """
print(json.dumps({
    "type": "tool_use", "timestamp": 1, "sessionID": "fake-analysis",
    "part": {"type": "tool", "tool": "read", "state": {
        "status": "completed", "input": {"filePath": "README.md"}, "output": "hello"}},
}))
print(json.dumps({
    "type": "text", "timestamp": 2, "sessionID": "fake-analysis",
    "part": {"type": "text", "text": "analysis done"},
}))
"""

_NO_EVIDENCE = """
print(json.dumps({
    "type": "text", "timestamp": 1, "sessionID": "fake-analysis",
    "part": {"type": "text", "text": "answered without reading anything"},
}))
"""

_DENIED = """
print(json.dumps({
    "type": "tool_use", "timestamp": 1, "sessionID": "fake-analysis",
    "part": {"type": "tool", "tool": "shell", "state": {
        "status": "error", "input": {"command": "ls"}, "error": "Permission denied: shell"}},
}))
print(json.dumps({
    "type": "text", "timestamp": 2, "sessionID": "fake-analysis",
    "part": {"type": "text", "text": "blocked"},
}))
"""

_WRONG_POLICY = """
print(json.dumps([
    {"type": "document", "path": "/repo/opencode.json",
     "info": {"permissions": [{"action": "shell", "resource": "*", "effect": "allow"}]}},
]))
sys.exit(0)
"""


def _indent(text: str) -> str:
    return "\n".join("    " + line if line.strip() else line for line in text.splitlines())


def _write_fake(
    tmp_path: Path,
    run_body: str,
    *,
    debug_body: str = _DEBUG_CONFIG,
    extra_imports: str = "",
    name: str = "fake-opencode",
) -> Path:
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        f"{extra_imports}\n"
        "args = sys.argv[1:]\n"
        'if args[:2] == ["debug", "config"]:\n'
        f"{_indent(debug_body)}\n"
        f"{run_body}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _sandbox_ready() -> None:
    capabilities = detect_sandbox_capabilities()
    if not capabilities.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {capabilities.detail}")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "GitBot Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    (path / "app.py").write_text("print('one')\n", encoding="utf-8")
    _git(path, "add", "README.md", "app.py")
    _git(path, "commit", "-q", "-m", "initial")
    return path


def _engine(
    tmp_path: Path,
    fake: Path,
    *,
    timeout_s: float = 30.0,
    executable: Path | None = None,
) -> TaskEngine:
    _sandbox_ready()
    database = Database(tmp_path / "gitbot.db")
    database.migrate()
    adapter = OpenCodeAdapter(
        OpenCodeConfig(executable=str(executable or fake), timeout_s=timeout_s)
    )
    return TaskEngine(
        database,
        AuditLog(database),
        opencode=adapter,
        dry_run=True,
        allowed_roots=(tmp_path,),
    )


def _add_repo(engine: TaskEngine, repo: Path) -> int:
    return RepoStore(engine.db).add(path=str(repo), slug="local/repo").id


def _assert_untouched(repo: Path) -> None:
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert _git(repo, "branch", "--list", "gitbot/*").strip() == ""
    assert not (repo / "opencode.json").exists()


def test_analysis_is_read_only_and_ends_analyzed(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, _SUCCESS)
    engine = _engine(tmp_path, fake)
    repo_id = _add_repo(engine, repo)
    head = _git(repo, "rev-parse", "HEAD").strip()

    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "summarize this project")
    task = engine.run_task(task_id)

    assert task["state"] == "analyzed"
    assert task["sandboxed"] == 1
    assert task["snapshot_head"] == head
    assert task["output"] == "analysis done"
    assert task["approval_id"] is None
    assert task["diff_hash"] is None
    assert task["diff_patch"] is None
    assert task["finished_at"]

    with engine.db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        events = [
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ]
    assert "task.analyzed" in events
    _assert_untouched(repo)


def test_analysis_fails_closed_without_repository_evidence(tmp_path: Path, repo: Path) -> None:
    """A text-only answer is not evidence: the task must not become analyzed."""
    fake = _write_fake(tmp_path, _NO_EVIDENCE)
    engine = _engine(tmp_path, fake)
    repo_id = _add_repo(engine, repo)

    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "answer from memory")
    with pytest.raises(GitBotError, match="no repository evidence"):
        engine.run_task(task_id)

    task = engine.get_task(task_id)
    assert task["state"] == "failed"
    assert task["approval_id"] is None
    assert task["output"] == "answered without reading anything"
    assert "no repository evidence" in task["error_message"]
    _assert_untouched(repo)


def test_analysis_fails_closed_on_permission_denial(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, _DENIED)
    engine = _engine(tmp_path, fake)
    repo_id = _add_repo(engine, repo)

    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "try to run a command")
    with pytest.raises(GitBotError, match="permission denied"):
        engine.run_task(task_id)

    task = engine.get_task(task_id)
    assert task["state"] == "failed"
    assert "permission denied" in task["error_message"]
    _assert_untouched(repo)


def test_analysis_fails_closed_when_policy_is_not_loaded(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, _SUCCESS, debug_body=_WRONG_POLICY)
    engine = _engine(tmp_path, fake)
    repo_id = _add_repo(engine, repo)

    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "analyse")
    with pytest.raises(GitBotError, match="permissions"):
        engine.run_task(task_id)

    task = engine.get_task(task_id)
    assert task["state"] == "failed"
    _assert_untouched(repo)


def test_analysis_reports_process_errors(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, 'print("boom", file=sys.stderr)\nsys.exit(1)')
    engine = _engine(tmp_path, fake)
    repo_id = _add_repo(engine, repo)

    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "analyse")
    with pytest.raises(GitBotError, match="analyze failed"):
        engine.run_task(task_id)

    task = engine.get_task(task_id)
    assert task["state"] == "failed"
    assert "boom" in task["error_message"]
    _assert_untouched(repo)


def test_analysis_refuses_a_dirty_working_tree(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, _SUCCESS)
    engine = _engine(tmp_path, fake)
    repo_id = _add_repo(engine, repo)
    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "analyse")
    with pytest.raises(GitBotError, match="preflight failed"):
        engine.run_task(task_id)

    task = engine.get_task(task_id)
    assert task["state"] == "failed"
    assert "uncommitted changes" in task["error_message"]
    # The user's untracked file must not be touched by GitBot.
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "dirty\n"
    assert _git(repo, "branch", "--list", "gitbot/*").strip() == ""


def test_analysis_can_be_cancelled(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, "time.sleep(30)", extra_imports="import time")
    engine = _engine(tmp_path, fake, timeout_s=60)
    repo_id = _add_repo(engine, repo)
    task_id = engine.create_task(repo_id, TaskType.ANALYZE, "slow analysis")

    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(engine.run_task(task_id)))
    worker.start()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if engine.get_task(task_id)["state"] == "running":
            break
        time.sleep(0.05)
    else:  # pragma: no cover - defensive
        pytest.fail("analysis never reached the running state")

    engine.cancel_task(task_id)
    worker.join(timeout=20)
    assert not worker.is_alive()

    task = engine.get_task(task_id)
    assert task["state"] == "cancelled"
    assert task["sandboxed"] == 1
    _assert_untouched(repo)


def test_run_analysis_returns_classified_result(tmp_path: Path, repo: Path) -> None:
    fake = _write_fake(tmp_path, _SUCCESS)
    result = run_analysis(
        repo,
        "analyse",
        allowed_roots=(tmp_path,),
        config=OpenCodeConfig(executable=str(fake), timeout_s=30),
    )

    assert result.classification is RunClassification.SUCCESS
    assert result.output == "analysis done"
    assert result.snapshot_head == _git(repo, "rev-parse", "HEAD").strip()
    assert result.session_id == "fake-analysis"


def test_analysis_policy_is_read_only() -> None:
    assert (
        PermissionRule("read", "*", "allow"),
        PermissionRule("edit", "*", "deny"),
        PermissionRule("write", "*", "deny"),
        PermissionRule("shell", "*", "deny"),
        PermissionRule("execute", "*", "deny"),
    ) == ANALYSIS_RULES
