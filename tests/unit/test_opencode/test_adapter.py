"""Tests for OpenCode adapter with fake executables."""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

import pytest

from gitbot.errors import PolicyViolation
from gitbot.opencode.adapter import (
    OpenCodeAdapter,
    OpenCodeConfig,
    RunClassification,
    count_completed_tool_calls,
)
from gitbot.opencode.parser import OpenCodeEventParser


def _write_fake(tmp_path: Path, name: str, script: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + script + "\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_adapter_success(tmp_path: Path) -> None:
    script = (
        "cat << 'EOF'\n"
        '{"type":"step_start","timestamp":1,"sessionID":"s1","part":{"type":"step-start"}}\n'
        '{"type":"tool_use","timestamp":2,"sessionID":"s1","part":{"type":"tool","tool":"shell",'
        '"state":{"status":"completed","input":{"command":"echo hi"},"output":"hi"}}}\n'
        '{"type":"step_finish","timestamp":3,"sessionID":"s1","part":{"type":"step-finish",'
        '"reason":"tool-calls"}}\n'
        '{"type":"text","timestamp":4,"sessionID":"s1","part":{"type":"text","text":"done"}}\n'
        "EOF\n"
        "exit 0\n"
    )
    fake = _write_fake(tmp_path, "opencode", script)
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=5))
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)
    assert res.classification == RunClassification.SUCCESS
    assert res.session_id == "s1"


def test_adapter_resource_permission_denied(tmp_path: Path) -> None:
    script = (
        "cat << 'EOF'\n"
        '{"type":"tool_use","timestamp":1,"sessionID":"s1","part":{"type":"tool","tool":"shell",'
        '"state":{"status":"error","input":{"command":"rm -rf /"},'
        '"error":"Permission denied: shell"}}}\n'
        '{"type":"text","timestamp":2,"sessionID":"s1","part":{"type":"text","text":"cannot"}}\n'
        "EOF\n"
        "exit 0\n"
    )
    fake = _write_fake(tmp_path, "opencode", script)
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=5))
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)
    assert res.classification == RunClassification.PERMISSION_DENIED
    assert res.denied_actions


def test_adapter_startup_error_nonzero_no_json(tmp_path: Path) -> None:
    fake = _write_fake(
        tmp_path,
        "opencode",
        """echo "unknown flag" >&2
echo "Usage: opencode ..." >&1
exit 1
""",
    )
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=5))
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)
    assert res.classification == RunClassification.PROCESS_ERROR
    assert res.exit_code == 1


def test_adapter_provider_error(tmp_path: Path) -> None:
    script = (
        'echo \'{"type":"error","timestamp":1,"sessionID":"s1",'
        '"error":{"type":"provider.no-route","message":"Model unavailable"}}\'\n'
        "exit 1\n"
    )
    fake = _write_fake(tmp_path, "opencode", script)
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=5))
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)
    assert res.classification == RunClassification.PROCESS_ERROR


def test_adapter_timeout(tmp_path: Path) -> None:
    fake = _write_fake(
        tmp_path,
        "opencode",
        """sleep 10
echo '{"type":"text","timestamp":1,"sessionID":"s1","part":{"type":"text","text":"late"}}'
exit 0
""",
    )
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=1, kill_grace_s=1))
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)
    assert res.classification == RunClassification.TIMEOUT


def test_adapter_redacts_assistant_output(tmp_path: Path) -> None:
    leaked = "gh" + "p_" + ("A1b2C3d4E5f6G7h8I9j0" * 2)
    script = (
        "cat << 'EOF'\n"
        '{"type":"text","timestamp":1,"sessionID":"s1","part":{"type":"text","text":"'
        + leaked
        + '"}}\n'
        "EOF\n"
        "exit 0\n"
    )
    fake = _write_fake(tmp_path, "opencode", script)
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=5))
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)

    assert res.classification == RunClassification.SUCCESS
    assert res.output is not None
    assert leaked not in res.output
    assert "[REDACTED:github_token]" in res.output
    assert res.output_truncated is False


def test_adapter_truncates_assistant_output(tmp_path: Path) -> None:
    script = (
        "cat << 'EOF'\n"
        '{"type":"text","timestamp":1,"sessionID":"s1","part":{"type":"text","text":"'
        "abcdefghijklmnop"
        '"}}\n'
        "EOF\n"
        "exit 0\n"
    )
    fake = _write_fake(tmp_path, "opencode", script)
    adapter = OpenCodeAdapter(
        OpenCodeConfig(executable=str(fake), timeout_s=5, max_output_chars=10)
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    res = adapter.run("test", cwd=repo, allow_unsandboxed=True)

    assert res.output == "abcdefghij"
    assert res.output_truncated is True


def test_adapter_pins_agent_and_model(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
    script = (
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" > "$FAKE_ARGV_LOG"\n'
        'echo \'{"type":"text","timestamp":1,"sessionID":"s1",'
        '"part":{"type":"text","text":"ok"}}\'\n'
        "exit 0\n"
    )
    fake = _write_fake(tmp_path, "opencode", script.replace("#!/bin/bash\n", ""))
    adapter = OpenCodeAdapter(
        OpenCodeConfig(executable=str(fake), timeout_s=5, model="provider/model", agent="build")
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    adapter.run("test", cwd=repo, allow_unsandboxed=True)

    recorded = log.read_text(encoding="utf-8").splitlines()
    assert "--model" in recorded
    assert "provider/model" in recorded
    assert "--agent" in recorded
    assert "build" in recorded
    assert "--auto" not in recorded


def test_adapter_cancellation_kills_process_group(tmp_path: Path) -> None:
    fake = _write_fake(tmp_path, "opencode", "sleep 10\nexit 0\n")
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=30))
    repo = tmp_path / "repo"
    repo.mkdir()
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    res = adapter.run("test", cwd=repo, cancel_event=cancel, allow_unsandboxed=True)
    elapsed = time.monotonic() - started

    assert res.classification == RunClassification.CANCELLED
    assert elapsed < 5


def test_adapter_cancellation_escalates_when_term_is_ignored(tmp_path: Path) -> None:
    fake = _write_fake(
        tmp_path,
        "opencode",
        """trap '' TERM
sleep 30
exit 0
""",
    )
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=30, kill_grace_s=0.5))
    repo = tmp_path / "repo"
    repo.mkdir()
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        res = adapter.run("test", cwd=repo, cancel_event=cancel, allow_unsandboxed=True)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    assert res.classification == RunClassification.CANCELLED
    assert elapsed < 5


def _events(*lines: str):
    parser = OpenCodeEventParser()
    return parser.parse_stream("\n".join(lines))


def test_count_completed_tool_calls_counts_only_successful_tool_uses() -> None:
    assert count_completed_tool_calls([]) == 0
    assert (
        count_completed_tool_calls(
            _events(
                '{"type":"text","timestamp":1,"sessionID":"s","part":{"type":"text","text":"hi"}}'
            )
        )
        == 0
    )

    events = _events(
        '{"type":"tool_use","timestamp":1,"sessionID":"s","part":{"type":"tool","tool":"read",'
        '"state":{"status":"completed","input":{},"output":"ok"}}}',
        '{"type":"tool_use","timestamp":2,"sessionID":"s","part":{"type":"tool","tool":"read",'
        '"state":{"status":"error","input":{},"error":"permission denied: read"}}}',
        '{"type":"tool_use","timestamp":3,"sessionID":"s","part":{"type":"tool","tool":"read",'
        '"state":{"status":"running","input":{}}}}',
        '{"type":"text","timestamp":4,"sessionID":"s","part":{"type":"text","text":"done"}}',
    )
    assert count_completed_tool_calls(events) == 1


def test_unsandboxed_run_requires_an_explicit_opt_in(tmp_path: Path) -> None:
    """Task lanes must never reach raw host execution by accident."""
    adapter = OpenCodeAdapter(OpenCodeConfig(executable=str(tmp_path / "missing-opencode")))
    with pytest.raises(PolicyViolation, match="unsandboxed OpenCode execution is disabled"):
        adapter.run("hello", cwd=tmp_path)
