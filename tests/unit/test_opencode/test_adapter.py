"""Tests for OpenCode adapter with fake executables."""

from __future__ import annotations

import stat
from pathlib import Path

from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig, RunClassification


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
    res = adapter.run("test", cwd=repo)
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
    res = adapter.run("test", cwd=repo)
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
    res = adapter.run("test", cwd=repo)
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
    res = adapter.run("test", cwd=repo)
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
    res = adapter.run("test", cwd=repo)
    assert res.classification == RunClassification.TIMEOUT
