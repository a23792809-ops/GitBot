"""End-to-end tests for the loopback-to-Unix-socket sandbox relay.

The relay is the only network path out of the repository sandbox.  These tests
use real bubblewrap and a real relay process, but the host side is either a tiny
echo server or the authenticated gateway with a fake model runner.  No provider,
network, credential, or repository is involved.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from gitbot.opencode.gateway import (
    RELAY_SCRIPT,
    SANDBOX_GATEWAY_SOCKET,
    SANDBOX_RELAY_SCRIPT,
    SANDBOX_UPSTREAM_SOCKET,
    AIGateway,
    GatewayConfig,
    prepare_sandbox_command,
)
from gitbot.security.sandbox import (
    build_sandbox_command,
    detect_sandbox_capabilities,
)
from gitbot.security.sandbox_relay import _parse_listen, main

ECHO_PORT = 18090
GATEWAY_PORT = 18091


class _FakeRunner:
    def __init__(self, answer: str) -> None:
        self.answer = answer
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
        return json.dumps({"action": "final", "content": self.answer})


def _sandbox_ready() -> None:
    capabilities = detect_sandbox_capabilities()
    if not capabilities.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {capabilities.detail}")


def _sandbox_roots(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()
    (snapshot / "README.md").write_text("hello\n", encoding="utf-8")
    return snapshot, workspace


def _run_sandbox(
    spec: object,
    *,
    timeout_s: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        spec.argv,  # type: ignore[attr-defined]
        env=dict(spec.host_env),  # type: ignore[attr-defined]
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


class _EchoServer:
    """Minimal Unix-socket echo server used to prove byte forwarding."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._connections: list[threading.Thread] = []

    def __enter__(self) -> _EchoServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2.0)
        for thread in self._connections:
            thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._echo, args=(client,), daemon=True)
            thread.start()
            self._connections.append(thread)

    @staticmethod
    def _echo(client: socket.socket) -> None:
        try:
            while True:
                data = client.recv(64 * 1024)
                if not data:
                    return
                client.sendall(data)
        except OSError:
            return
        finally:
            client.close()


def test_relay_listen_address_must_be_loopback() -> None:
    assert _parse_listen("127.0.0.1:18080") == ("127.0.0.1", 18080)
    assert _parse_listen("localhost:1") == ("localhost", 1)
    assert _parse_listen("::1:65535") == ("::1", 65535)

    for raw in ("0.0.0.0:18080", "192.168.1.5:18080", "127.0.0.1", "127.0.0.1:x"):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_listen(raw)


def test_relay_main_validates_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--listen", "127.0.0.1:18080", "--unix", str(tmp_path / "x.sock")]) == 2
    assert "missing child command" in capsys.readouterr().err

    assert main(["--listen", "127.0.0.1:18080", "--unix", "relative.sock", "--", "true"]) == 2
    assert "absolute path" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--listen", "0.0.0.0:18080", "--unix", str(tmp_path / "x.sock"), "--", "true"])


def test_relay_runs_the_fixed_child_argv(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = main(
        [
            "--listen",
            f"127.0.0.1:{port}",
            "--unix",
            str(tmp_path / "unused.sock"),
            "--",
            "python3",
            "-c",
            "print('child-ok')",
        ]
    )
    assert result == 0


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_sandbox_relay_forwards_loopback_bytes_and_blocks_the_host_network(
    tmp_path: Path,
) -> None:
    snapshot, workspace = _sandbox_roots(tmp_path)
    socket_dir = tmp_path / "gitbot-upstream"
    socket_dir.mkdir(mode=0o700)
    upstream = socket_dir / "upstream.sock"
    client = f"""
import json
import socket

result = {{}}
with socket.create_connection(("127.0.0.1", {ECHO_PORT}), timeout=5) as connection:
    connection.sendall(b"ping")
    result["echo"] = connection.recv(4).decode("utf-8")
try:
    socket.create_connection(("1.1.1.1", 80), timeout=2).close()
    result["network"] = "allowed"
except OSError:
    result["network"] = "denied"
print(json.dumps(result))
"""

    with _EchoServer(upstream):
        spec = build_sandbox_command(
            snapshot,
            workspace,
            [
                "python3",
                SANDBOX_RELAY_SCRIPT,
                "--listen",
                f"127.0.0.1:{ECHO_PORT}",
                "--unix",
                SANDBOX_UPSTREAM_SOCKET,
                "--",
                "python3",
                "-c",
                client,
            ],
            extra_ro_binds=[(RELAY_SCRIPT, SANDBOX_RELAY_SCRIPT)],
            extra_rw_binds=[(upstream, SANDBOX_UPSTREAM_SOCKET)],
        )
        completed = _run_sandbox(spec)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"echo": "ping", "network": "denied"}


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_gateway_request_round_trips_through_the_sandbox_relay(tmp_path: Path) -> None:
    snapshot, workspace = _sandbox_roots(tmp_path)
    runner = _FakeRunner("relay says hi")
    gateway = AIGateway(
        GatewayConfig(enabled=True, relay_port=GATEWAY_PORT),
        runner=runner,
    )
    gateway.start()
    try:
        endpoint = gateway.endpoint
        token = endpoint.policy.api_key
        client = f"""
import json
import socket
import urllib.error
import urllib.request

TOKEN = {token!r}

def chat(authorized):
    body = json.dumps({{
        "model": "gitbot",
        "messages": [{{"role": "user", "content": "hello through relay"}}],
    }}).encode("utf-8")
    headers = {{"Content-Type": "application/json"}}
    if authorized:
        headers["Authorization"] = "Bearer " + TOKEN
    request = urllib.request.Request(
        "http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())

bad_status, bad_body = chat(False)
ok_status, ok_body = chat(True)
try:
    socket.create_connection(("1.1.1.1", 80), timeout=2).close()
    network = "allowed"
except OSError:
    network = "denied"
print(json.dumps({{
    "bad_status": bad_status,
    "bad_code": bad_body.get("error", {{}}).get("code"),
    "ok_status": ok_status,
    "content": ok_body["choices"][0]["message"]["content"],
    "network": network,
}}))
"""

        argv, ro_binds = prepare_sandbox_command(
            endpoint,
            ["python3", "-c", client],
            executable="python3",
        )
        spec = build_sandbox_command(
            snapshot,
            workspace,
            argv,
            extra_ro_binds=ro_binds,
            extra_rw_binds=endpoint.rw_binds,
        )
        completed = _run_sandbox(spec)
    finally:
        gateway.stop()

    assert runner.started == 1
    assert runner.stopped == 1
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["bad_status"] == 401
    assert result["bad_code"] == "unauthorized"
    assert result["ok_status"] == 200
    assert result["content"] == "relay says hi"
    assert result["network"] == "denied"
    assert "hello through relay" in runner.prompts[0]
    assert SANDBOX_GATEWAY_SOCKET in spec.argv
