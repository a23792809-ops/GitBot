"""End-to-end AI workflow through the real sandbox relay and a fake provider.

The repository sandbox, the relay, the authenticated Unix-socket gateway, and
the TaskEngine/API workflow are real.  Only the host model runner is replaced by
a deterministic fake, so no provider credential, network call, or quota is used.
"""

from __future__ import annotations

import json
import socket
import subprocess
import textwrap
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gitbot.api.app import create_app
from gitbot.api.routes import tasks as tasks_routes
from gitbot.audit import AuditLog
from gitbot.config import load_settings
from gitbot.db import Database
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.opencode import gateway as gateway_module
from gitbot.opencode.adapter import OpenCodeAdapter, OpenCodeConfig
from gitbot.opencode.gateway import GatewayConfig, GatewayError, GatewayProviderError
from gitbot.repos import RepoStore
from gitbot.security.sandbox import detect_sandbox_capabilities
from gitbot.tasks.engine import TaskEngine

TOKEN = "x" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}

_FAKE_GATEWAY_OPENCODE = r'''#!/usr/bin/env python3
"""Hermetic OpenCode stand-in that talks to the real GitBot gateway."""
import json
import os
import sys
import urllib.error
import urllib.request

args = sys.argv[1:]
config_path = os.path.join(os.getcwd(), "opencode.json")


def load_config():
    with open(config_path, encoding="utf-8") as handle:
        return json.load(handle)


def provider_parts(config):
    provider_id, provider = next(iter(config["provider"].items()))
    model_id = config["model"].split("/", 1)[1]
    return provider_id, provider, model_id


if args[:2] == ["debug", "config"]:
    config = load_config()
    provider_id, provider, model_id = provider_parts(config)
    info = {
        "permissions": config["permissions"],
        "model": {"providerID": provider_id, "model": model_id},
        "providers": {
            provider_id: {
                "package": "aisdk:" + provider["npm"],
                "settings": {
                    "baseURL": provider["options"]["baseURL"],
                    "apiKey": "***",
                },
                "models": provider["models"],
            }
        },
    }
    print(json.dumps([
        {"type": "directory", "path": "/work/config/opencode"},
        {"type": "document", "path": config_path, "info": info},
    ]))
    sys.exit(0)

config = load_config()
_provider_id, provider, _model_id = provider_parts(config)
options = provider["options"]
base_url = options["baseURL"].rstrip("/")
token = options["apiKey"]
prompt = args[-1] if args else ""
TOOLS = [{
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object"},
    },
}]


def chat(messages):
    body = json.dumps({
        "model": "gitbot",
        "messages": messages,
        "tools": TOOLS,
    }).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fail(error_type, message):
    print(json.dumps({
        "type": "error",
        "error": {"type": error_type, "message": message},
    }))
    sys.exit(1)


try:
    payload = chat([{"role": "user", "content": prompt}])
except urllib.error.HTTPError as exc:
    raw = exc.read().decode("utf-8", "replace")
    try:
        error = json.loads(raw).get("error", {})
    except json.JSONDecodeError:
        error = {}
    fail(str(error.get("code", "gateway_error")), str(error.get("message", raw)))
except Exception as exc:  # noqa: BLE001 - surface a structured failure
    fail("gateway_error", str(exc))

message = payload["choices"][0]["message"]
tool_calls = message.get("tool_calls")
executed_tool = False
tool_name = "read"
if tool_calls:
    # Execute the first read tool exactly as a real client would, then send
    # the result back to the model for the next decision.
    call = tool_calls[0]
    name = call.get("function", {}).get("name")
    if isinstance(name, str) and name:
        tool_name = name
    executed_tool = True
    try:
        arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}
    path = arguments.get("path") or arguments.get("file_path") or "README.md"
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    try:
        with open(path, encoding="utf-8") as handle:
            tool_content = handle.read()
    except OSError as exc:
        tool_content = "tool error: " + str(exc)
    follow_up = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
        {
            "role": "tool",
            "tool_call_id": call.get("id", "call_1"),
            "name": name,
            "content": tool_content,
        },
    ]
    try:
        payload = chat(follow_up)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            error = json.loads(raw).get("error", {})
        except json.JSONDecodeError:
            error = {}
        fail(str(error.get("code", "gateway_error")), str(error.get("message", raw)))
    except Exception as exc:  # noqa: BLE001 - surface a structured failure
        fail("gateway_error", str(exc))
    message = payload["choices"][0]["message"]

content = message.get("content")
if not isinstance(content, str):
    content = "gateway returned a tool call"
if os.getcwd().startswith("/work"):
    with open("gateway-output.txt", "w", encoding="utf-8") as handle:
        handle.write(content)
events = []
if executed_tool:
    # A real OpenCode emits a terminal tool_use event for every executed tool.
    events.append({
        "type": "tool_use",
        "timestamp": 1,
        "sessionID": "gateway-session",
        "part": {
            "type": "tool",
            "tool": tool_name,
            "state": {"status": "completed", "input": {}, "output": "ok"},
        },
    })
events.append({
    "type": "text",
    "timestamp": 2,
    "sessionID": "gateway-session",
    "part": {"type": "text", "text": content},
})
for event in events:
    print(json.dumps(event))
'''


class FakeHostRunner:
    """A fake host model runner with the same lifecycle as HostModelRunner."""

    def __init__(
        self,
        content: str = "gateway result",
        error: BaseException | None = None,
        outputs: list[str] | None = None,
    ) -> None:
        self.content = content
        self.error = error
        self.outputs = list(outputs) if outputs is not None else None
        self.prompts: list[str] = []
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def complete(
        self,
        prompt: str,
        *,
        timeout_s: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        if self.outputs is not None:
            if not self.outputs:
                raise AssertionError("fake host runner ran out of scripted answers")
            return self.outputs.pop(0)
        # Default: obey the gateway protocol by requesting a repository tool
        # first, then answering once the result has come back.
        if len(self.prompts) == 1:
            return json.dumps(
                {"action": "tool", "name": "read", "arguments": {"path": "README.md"}}
            )
        return json.dumps({"action": "final", "content": self.content})


def _sandbox_ready() -> None:
    capabilities = detect_sandbox_capabilities()
    if not capabilities.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {capabilities.detail}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path, files: dict[str, str] | None = None) -> None:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "GitBot Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    for name, content in (files or {}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")


def _write_fake_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "fake-gateway-opencode"
    script.write_text(textwrap.dedent(_FAKE_GATEWAY_OPENCODE), encoding="utf-8")
    script.chmod(0o755)
    return script


def _seed_repo(
    tmp_path: Path,
    db: Database,
    *,
    allow_commit: bool = False,
    test_command: str | None = None,
    files: dict[str, str] | None = None,
) -> int:
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, files)
    return (
        RepoStore(db)
        .add(
            path=str(repo_dir),
            slug="local/gateway-repo",
            allow_commit=allow_commit,
            test_command=test_command,
        )
        .id
    )


@contextmanager
def _build_client(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner_content: str = "gateway result",
    runner_error: BaseException | None = None,
    runner_outputs: list[str] | None = None,
) -> Iterator[tuple[TestClient, Database, list[FakeHostRunner]]]:
    _sandbox_ready()
    port = _free_port()
    settings = load_settings(
        env={
            "HOME": str(tmp_path),
            "GITBOT_DRY_RUN": "false",
            "GITBOT_AI_GATEWAY_ENABLED": "true",
            "GITBOT_AI_GATEWAY_RELAY_PORT": str(port),
            "GITBOT_RETRY_DELAY_S": "0",
        }
    )
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(TOKEN + "\n", encoding="utf-8")
    settings.token_file.chmod(0o600)

    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    fake = _write_fake_opencode(tmp_path)
    engine = TaskEngine(
        database,
        audit,
        opencode=OpenCodeAdapter(OpenCodeConfig(executable=str(fake), timeout_s=60.0)),
        dry_run=False,
        allowed_roots=(tmp_path,),
        gateway=GatewayConfig(
            enabled=True,
            relay_port=port,
            timeout_s=60.0,
        ),
        retry_delay_s=0,
    )

    runners: list[FakeHostRunner] = []

    def runner_factory(_config: GatewayConfig) -> FakeHostRunner:
        runner = FakeHostRunner(runner_content, runner_error, runner_outputs)
        runners.append(runner)
        return runner

    monkeypatch.setattr(gateway_module, "HostModelRunner", runner_factory)
    service = GithubService(GhClient(str(fake_gh), timeout_s=10), audit=audit, ttl_s=60)
    app = create_app(settings=settings, database=database, github=service, audit=audit)
    app.dependency_overrides[tasks_routes._get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, database, runners


def _assert_repo_untouched(repo: Path) -> None:
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert not (repo / "opencode.json").exists()


@pytest.mark.parametrize(
    ("task_type", "terminal_state"),
    [("analyze", "analyzed"), ("explain", "explained")],
)
def test_read_only_tasks_use_the_gateway(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
    terminal_state: str,
) -> None:
    with _build_client(
        tmp_path, fake_gh, monkeypatch, runner_content="gateway analysis result"
    ) as (client, db, runners):
        repo_id = _seed_repo(tmp_path, db)
        created = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": task_type,
                "prompt": "summarize the request flow",
            },
            headers=HEADERS,
        )
        assert created.status_code == 200, created.json()
        task_id = created.json()["task"]["id"]

        response = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert response.status_code == 200, response.json()
        task = response.json()["task"]
        assert task["state"] == terminal_state
        assert task["sandboxed"] == 1
        assert task["output"] == "gateway analysis result"
        assert task["approval_id"] is None

        assert len(runners) == 1
        assert runners[0].started == 1
        assert runners[0].stopped == 1
        assert runners[0].prompts
        assert "summarize the request flow" in runners[0].prompts[0]
        assert TOKEN not in runners[0].prompts[0]
        _assert_repo_untouched(tmp_path / "repo")


@pytest.mark.parametrize("task_type", ["bugfix", "feature", "docs", "deps", "tests"])
def test_every_write_task_type_runs_through_the_gateway(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
) -> None:
    with _build_client(tmp_path, fake_gh, monkeypatch, runner_content="gateway change result") as (
        client,
        db,
        runners,
    ):
        repo_id = _seed_repo(tmp_path, db, test_command="/bin/true")
        created = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": task_type,
                "prompt": "make the requested change",
            },
            headers=HEADERS,
        )
        assert created.status_code == 200, created.json()
        task_id = created.json()["task"]["id"]

        response = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert response.status_code == 200, response.json()
        task = response.json()["task"]
        assert task["state"] == "awaiting_review"
        assert task["sandboxed"] == 1
        assert task["tests_status"] == "passed"
        assert task["approval_id"]

        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert "gateway-output.txt" in detail["diff"]["patch"]
        assert "gateway change result" in detail["diff"]["patch"]
        assert detail["diff"]["stored"] is True

        assert len(runners) == 1
        assert runners[0].prompts
        assert "make the requested change" in runners[0].prompts[0]
        _assert_repo_untouched(tmp_path / "repo")


def test_gateway_change_can_be_approved_and_committed(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_client(
        tmp_path, fake_gh, monkeypatch, runner_content="committed gateway output"
    ) as (client, db, _runners):
        repo_id = _seed_repo(tmp_path, db, allow_commit=True, test_command="/bin/true")
        created = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "feature",
                "prompt": "add the gateway output",
            },
            headers=HEADERS,
        )
        assert created.status_code == 200, created.json()
        task_id = created.json()["task"]["id"]

        task = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS).json()["task"]
        assert task["state"] == "awaiting_review"

        approved = client.post(
            f"/api/tasks/approvals/{task['approval_id']}/approve",
            json={"diff_hash": task["diff_hash"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200, approved.json()
        assert (
            client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["task"]["state"]
            == "approved"
        )

        committed = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert committed.status_code == 200, committed.json()
        assert committed.json()["task"]["state"] == "committed"

        repo = tmp_path / "repo"
        assert (repo / "gateway-output.txt").read_text(encoding="utf-8") == (
            "committed gateway output"
        )
        assert _git(repo, "status", "--porcelain").strip() == ""
        log = _git(repo, "log", "-1", "--format=%s").strip()
        assert log.startswith("gitbot: feature task")

        duplicate = client.post(f"/api/tasks/{task_id}/commit", headers=HEADERS)
        assert duplicate.status_code == 409


def test_provider_failure_fails_closed_without_an_approval(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "token=abcdefghijklmnopqrstuvwxyz"
    with _build_client(
        tmp_path,
        fake_gh,
        monkeypatch,
        runner_error=GatewayError(f"provider said {secret}"),
    ) as (client, db, _runners):
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "analyze",
                "prompt": "this provider will fail",
            },
            headers=HEADERS,
        ).json()["task"]["id"]

        response = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert response.status_code == 403
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "failed"
        assert detail["task"]["approval_id"] is None
        assert detail["diff"] is None
        assert secret not in (detail["task"]["error_message"] or "")
        assert "[REDACTED:" in (detail["task"]["error_message"] or "")
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0
        _assert_repo_untouched(tmp_path / "repo")


def test_multi_step_tool_call_round_trip(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sandbox executes a tool call and returns its result to the model."""
    with _build_client(
        tmp_path,
        fake_gh,
        monkeypatch,
        runner_outputs=[
            json.dumps(
                {
                    "action": "tool",
                    "name": "read",
                    "arguments": {"path": "/repo/main.py"},
                }
            ),
            json.dumps({"action": "final", "content": "main.py prints hello"}),
        ],
    ) as (client, db, runners):
        repo_id = _seed_repo(tmp_path, db, files={"main.py": "print('hello')\n"})
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "analyze",
                "prompt": "Explain what main.py prints.",
            },
            headers=HEADERS,
        ).json()["task"]["id"]

        response = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert response.status_code == 200, response.json()
        task = response.json()["task"]
        assert task["state"] == "analyzed"
        assert task["output"] == "main.py prints hello"

        assert len(runners) == 1
        assert len(runners[0].prompts) == 2
        assert "print('hello')" in runners[0].prompts[1]
        assert "TOOL ROUTING:" in runners[0].prompts[0]
        _assert_repo_untouched(tmp_path / "repo")


def test_provider_rate_limit_is_surfaced_clearly(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_client(
        tmp_path,
        fake_gh,
        monkeypatch,
        runner_error=GatewayProviderError(
            "host model provider rate limit reached; retry later",
            provider_code="rate_limit",
            http_status=429,
            code="ai_gateway_rate_limited",
        ),
    ) as (client, db, _runners):
        repo_id = _seed_repo(tmp_path, db)
        task_id = client.post(
            "/api/tasks",
            json={
                "repo_id": repo_id,
                "task_type": "analyze",
                "prompt": "this provider is rate limited",
            },
            headers=HEADERS,
        ).json()["task"]["id"]

        response = client.post(f"/api/tasks/{task_id}/run", headers=HEADERS)
        assert response.status_code == 429
        detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()
        assert detail["task"]["state"] == "failed"
        assert "rate limit" in (detail["task"]["error_message"] or "").lower()
        assert client.get("/api/tasks/approvals/pending", headers=HEADERS).json()["count"] == 0
        _assert_repo_untouched(tmp_path / "repo")
