"""Shared fixtures. All tests are hermetic: no real gh, git, or network calls."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database
from gitbot.security.tokens import ensure_token


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env={"HOME": str(tmp_path)})


@pytest.fixture
def auth_headers(settings: Settings) -> dict[str, str]:
    """Bearer header valid against the token file for ``settings``' home.

    Sensitive read endpoints require it, so tests that exercise them opt in
    explicitly instead of relying on an unauthenticated default.
    """
    token = ensure_token(settings.token_file)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "gitbot.db"


@pytest.fixture
def database(db_path: Path) -> Iterator[Database]:
    database = Database(db_path)
    database.migrate()
    yield database


@pytest.fixture
def audit_log(database: Database) -> AuditLog:
    return AuditLog(database)


@pytest.fixture
def fake_gh(tmp_path: Path) -> Iterator[Path]:
    """An executable stand-in for the real `gh`; no network, no credentials."""
    source = Path(__file__).parent / "fixtures" / "fake_gh.py"
    script = tmp_path / "fake-gh"
    script.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)
    yield script


_FAKE_OPENCODE = '''#!/usr/bin/env python3
"""Hermetic opencode stand-in: answers `debug config` and emits NDJSON events."""
import base64
import json
import os
import sys
import time

args = sys.argv[1:]
if args[:2] == ["debug", "config"]:
    path = os.path.join(os.getcwd(), "opencode.json")
    info = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            info = json.load(handle)
    print(json.dumps([{"type": "document", "path": path, "info": info}]))
    sys.exit(0)

text = os.environ.get("FAKE_OPENCODE_TEXT", "done")
session = os.environ.get("FAKE_OPENCODE_SESSION", "fake-session")
call_log = os.environ.get("FAKE_OPENCODE_ARGV_LOG")
if call_log:
    with open(call_log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
write = os.environ.get("FAKE_OPENCODE_WRITE")
writes = {}
deletes = []
symlinks = {}
binary = {}
probes = []
sleep_s = 0.0
prompt = args[-1] if args else ""
marker = "GITBOT_FAKE:"
if marker in prompt:
    start = prompt.index(marker) + len(marker)
    end = prompt.find("]]", start)
    if end != -1:
        try:
            directives = json.loads(prompt[start:end])
        except json.JSONDecodeError:
            directives = {}
        if isinstance(directives, dict):
            text = directives.get("text", text)
            write = directives.get("write", write)
            session = directives.get("session", session)
            writes = directives.get("writes", {})
            deletes = directives.get("deletes", [])
            symlinks = directives.get("symlinks", {})
            binary = directives.get("binary", {})
            probes = directives.get("probes", [])
            sleep_s = directives.get("sleep", 0.0)
if write:
    name, _, content = write.partition("::")
    with open(os.path.join(os.getcwd(), name), "w", encoding="utf-8") as handle:
        handle.write(content)
if isinstance(writes, dict):
    for name, content in writes.items():
        target = os.path.join(os.getcwd(), name)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
if isinstance(binary, dict):
    for name, content in binary.items():
        target = os.path.join(os.getcwd(), name)
        with open(target, "wb") as handle:
            handle.write(base64.b64decode(content))
if isinstance(deletes, list):
    for name in deletes:
        try:
            os.unlink(os.path.join(os.getcwd(), name))
        except FileNotFoundError:
            pass
if isinstance(symlinks, dict):
    for name, target_name in symlinks.items():
        os.symlink(target_name, os.path.join(os.getcwd(), name))
if isinstance(probes, list) and probes:
    text = json.dumps({str(name): os.path.exists(str(name)) for name in probes})
if sleep_s:
    time.sleep(float(sleep_s))
events = [
    {"type": "step_start", "timestamp": 1, "sessionID": session,
     "part": {"type": "step-start"}},
    {"type": "tool_use", "timestamp": 2, "sessionID": session,
     "part": {"type": "tool", "tool": "shell",
              "state": {"status": "completed", "input": {"command": "git status"},
                        "output": "ok"}}},
    {"type": "step_finish", "timestamp": 3, "sessionID": session,
     "part": {"type": "step-finish", "reason": "tool-calls"}},
    {"type": "text", "timestamp": 4, "sessionID": session,
     "part": {"type": "text", "text": text}},
]
for event in events:
    print(json.dumps(event))
'''


@pytest.fixture
def write_fake_opencode(tmp_path: Path):
    """Factory that installs the executable fake `opencode` and returns its path."""

    def _write(name: str = "opencode") -> Path:
        script = tmp_path / name
        script.write_text(_FAKE_OPENCODE, encoding="utf-8")
        script.chmod(0o755)
        return script

    return _write
