"""Tests for the host-side AI gateway and its sandbox relay contract.

Everything here is hermetic: fake model runners replace the real OpenCode
process, and the HTTP server is reached over its private Unix socket.  No
provider, network, credential, or repository is involved.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

from gitbot.errors import PolicyViolation
from gitbot.opencode import gateway as gateway_module
from gitbot.opencode.gateway import (
    DEFAULT_GATEWAY_ID,
    DEFAULT_GATEWAY_MODEL_ID,
    DEFAULT_HOST_RELAY_PORT,
    DEFAULT_RELAY_PORT,
    SANDBOX_GATEWAY_SOCKET,
    SANDBOX_RELAY_SCRIPT,
    SANDBOX_UPSTREAM_SOCKET,
    AIGateway,
    GatewayCancelledError,
    GatewayConfig,
    GatewayDecision,
    GatewayEndpoint,
    GatewayError,
    GatewayProtocolError,
    GatewayProviderError,
    GatewayRequestError,
    GatewayTimeoutError,
    HostModelRunner,
    HostTunnel,
    _classify_provider_error,
    _extract_host_text,
    _flatten_content,
    _has_tool_result,
    _host_agent_config,
    _host_used_tools,
    _normalize_messages,
    _normalize_tools,
    ai_gateway_session,
    build_gateway_prompt,
    parse_gateway_decision,
    prepare_sandbox_command,
    resolve_sandbox_executable,
    verify_host_agent_config,
)
from gitbot.opencode.policy import GatewayPolicy
from gitbot.security.sandbox import SandboxRunResult

TOKEN = "test-gateway-token-0123456789abcdef"


class FakeRunner:
    """Deterministic stand-in for :class:`HostModelRunner`."""

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.started = 0
        self.stopped = 0
        self.start_error: BaseException | None = None
        self.complete_error: BaseException | None = None

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
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
        if self.complete_error is not None:
            raise self.complete_error
        if not self.outputs:
            raise AssertionError("fake runner ran out of scripted answers")
        return self.outputs.pop(0)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path) -> None:
        super().__init__("gitbot.invalid")
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect(str(self._socket_path))
        self.sock = sock


@contextmanager
def _running_gateway(
    *outputs: str,
    config: GatewayConfig | None = None,
) -> Iterator[tuple[AIGateway, FakeRunner]]:
    runner = FakeRunner(*outputs)
    gateway = AIGateway(
        config or GatewayConfig(enabled=True),
        runner=runner,
    )
    gateway.start()
    try:
        yield gateway, runner
    finally:
        gateway.stop()


def _request(
    gateway: AIGateway,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    connection = _UnixHTTPConnection(gateway._socket_path)
    try:
        with suppress(BrokenPipeError):
            connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
    finally:
        connection.close()
    if "application/json" in content_type:
        return response.status, json.loads(raw)
    return response.status, raw


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _chat_body(**overrides: Any) -> bytes:
    payload: dict[str, Any] = {
        "model": "gitbot",
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_gateway_config_validation_rejects_unsafe_values() -> None:
    GatewayConfig().validate()

    invalid = [
        GatewayConfig(model=" "),
        GatewayConfig(model="inkling-2-small-free"),
        GatewayConfig(model="opencode/"),
        GatewayConfig(executable=" "),
        GatewayConfig(relay_port=0),
        GatewayConfig(relay_port=65536),
        GatewayConfig(timeout_s=0),
        GatewayConfig(kill_grace_s=-1),
        GatewayConfig(max_request_bytes=1023),
        GatewayConfig(max_prompt_chars=1023),
        GatewayConfig(max_output_chars=255),
    ]
    for config in invalid:
        with pytest.raises(GatewayError):
            config.validate()


def test_flatten_content_handles_text_parts_and_objects() -> None:
    assert _flatten_content("plain") == "plain"
    assert _flatten_content(None) == ""
    assert (
        _flatten_content(
            [
                "first",
                {"type": "text", "text": "second"},
                {"type": "input_text", "text": "third"},
                {"type": "image", "text": "ignored"},
                {"no": "text"},
            ]
        )
        == "first\nsecond\nthird\nignored"
    )
    assert _flatten_content({"a": 1}) == '{"a": 1}'


def test_normalize_messages_keeps_only_safe_fields() -> None:
    messages = _normalize_messages(
        [
            {
                "role": "system",
                "content": [{"type": "text", "text": "be brief"}],
                "extra": "dropped",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "a.py"}'},
                    },
                    {"id": "bad", "function": "not-a-dict"},
                    {"id": "bad2", "function": {"name": ""}},
                    {"id": "call_2", "function": {"name": "list", "arguments": "not json"}},
                ],
            },
            {"role": "tool", "content": "contents", "tool_call_id": "call_1", "name": "read"},
        ]
    )
    assert messages[0] == {"role": "system", "content": "be brief"}
    assert messages[1]["content"] == ""
    assert messages[1]["tool_calls"] == [
        {"id": "call_1", "name": "read", "arguments": {"path": "a.py"}},
        {"id": "call_2", "name": "list", "arguments": {"raw": "not json"}},
    ]
    assert messages[2] == {
        "role": "tool",
        "content": "contents",
        "tool_call_id": "call_1",
        "name": "read",
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "non-empty array"),
        ([], "non-empty array"),
        ([{"role": "user", "content": "x"}] * 201, "too many"),
        (["not-an-object"], "must be an object"),
        ([{"role": "root", "content": "x"}], "unsupported role"),
        ([{"role": "tool", "content": "x"}], "tool_call_id"),
        ([{"role": "user", "content": "x" * (2_000_001)}], "exceeds"),
    ],
)
def test_normalize_messages_rejects_invalid_requests(raw: Any, message: str) -> None:
    with pytest.raises(GatewayRequestError, match=message):
        _normalize_messages(raw)


def test_normalize_tools_validates_schemas_and_defaults() -> None:
    assert _normalize_tools(None) == []
    tools = _normalize_tools(
        [
            {
                "type": "function",
                "function": {"name": "read", "description": "Read a file"},
            }
        ]
    )
    assert tools == [
        {
            "name": "read",
            "description": "Read a file",
            "parameters": {"type": "object"},
        }
    ]

    invalid = [
        "not-a-list",
        [{"function": {"name": "x"}}] * 65,
        ["not-an-object"],
        [{"type": "function"}],
        [{"function": {"description": "no name"}}],
        [{"function": {"name": "x", "parameters": 5}}],
        [{"function": {"name": "x", "parameters": {"blob": "x" * 90_000}}}],
    ]
    for raw in invalid:
        with pytest.raises(GatewayRequestError):
            _normalize_tools(raw)


def test_build_gateway_prompt_marks_input_as_untrusted() -> None:
    prompt = build_gateway_prompt(
        [{"role": "user", "content": "summarize"}],
        [{"name": "read", "description": "", "parameters": {"type": "object"}}],
        max_chars=10_000,
    )
    assert "untrusted data" in prompt
    assert '"action":"final"' in prompt
    assert "summarize" in prompt
    assert '"name": "read"' in prompt

    with pytest.raises(GatewayRequestError) as caught:
        build_gateway_prompt(
            [{"role": "user", "content": "x" * 5000}],
            [],
            max_chars=1024,
        )
    assert caught.value.details["limit"] == 1024


def test_build_gateway_prompt_explains_tool_routing() -> None:
    prompt = build_gateway_prompt(
        [{"role": "user", "content": "what does main.py print?"}],
        [
            {
                "name": "read",
                "description": "Read a file from the repository",
                "parameters": {"type": "object"},
            },
            {
                "name": "grep",
                "description": "Search repository contents",
                "parameters": {"type": "object"},
            },
        ],
        max_chars=10_000,
    )
    assert "TOOL ROUTING:" in prompt
    assert "- read: Read a file from the repository" in prompt
    assert "- grep: Search repository contents" in prompt
    assert "select a read/search tool before answering" in prompt
    assert "Never claim you read a file without a tool result" in prompt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{"action":"final","answer":"aliased"}',
            GatewayDecision("final", content="aliased"),
        ),
        (
            '{"action":"final","output":"aliased output"}',
            GatewayDecision("final", content="aliased output"),
        ),
        (
            '{"action":"tool","tool":"read","args":{"path":"a.py"}}',
            GatewayDecision("tool", name="read", arguments={"path": "a.py"}),
        ),
        (
            '{"action":"tool","name":"read","arguments":"{\\"path\\":\\"a.py\\"}"}',
            GatewayDecision("tool", name="read", arguments={"path": "a.py"}),
        ),
    ],
)
def test_parse_gateway_decision_accepts_model_aliases(raw: str, expected: GatewayDecision) -> None:
    assert parse_gateway_decision(raw, allowed_tools=["read"]) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"action":"final","content":"hello"}', GatewayDecision("final", content="hello")),
        (
            'Here you go:\n```json\n{"action":"final","content":"fenced"}\n```\n',
            GatewayDecision("final", content="fenced"),
        ),
        (
            'prefix {"action":"tool","name":"read","arguments":{"path":"a"}} suffix',
            GatewayDecision("tool", name="read", arguments={"path": "a"}),
        ),
    ],
)
def test_parse_gateway_decision_accepts_valid_shapes(raw: str, expected: GatewayDecision) -> None:
    assert parse_gateway_decision(raw, allowed_tools=["read"]) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json at all", "JSON object"),
        ('{"action":"final","content":5}', "final content"),
        ('{"action":"tool","name":"write","arguments":{}}', "unknown tool"),
        ('{"action":"tool","name":"read","arguments":[]}', "tool arguments"),
        ('{"action":"tool","name":"read"}', "tool arguments"),
        ('{"action":"dance"}', "unknown action"),
        ('{"action":}', "invalid JSON"),
        ('{"action":"final","content":"x"', "JSON object"),
    ],
)
def test_parse_gateway_decision_rejects_invalid_answers(raw: str, message: str) -> None:
    with pytest.raises(GatewayError, match=message):
        parse_gateway_decision(raw, allowed_tools=["read"])


def test_extract_host_text_uses_only_text_events() -> None:
    stdout = "\n".join(
        [
            "not json",
            json.dumps({"type": "step_start"}),
            json.dumps({"type": "text", "part": {"text": "first"}}),
            json.dumps({"type": "text", "part": {"text": "second"}}),
            json.dumps({"type": "error", "error": {"message": "ignored warning"}}),
        ]
    )
    assert _extract_host_text(stdout) == "first\nsecond"

    with pytest.raises(GatewayError, match="failed"):
        _extract_host_text(json.dumps({"type": "error", "error": {"message": "boom"}}))
    with pytest.raises(GatewayError, match="no assistant text"):
        _extract_host_text(json.dumps({"type": "step_start"}))


def test_handle_chat_returns_final_text_and_usage() -> None:
    runner = FakeRunner('{"action":"final","content":"answer"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    response = gateway.handle_chat(
        {
            "model": "requested-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert response["object"] == "chat.completion"
    assert response["model"] == "requested-model"
    choice = response["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "answer"}
    assert choice["finish_reason"] == "stop"
    assert response["usage"]["prompt_tokens"] >= 1
    assert response["usage"]["completion_tokens"] >= 1
    assert "hello" in runner.prompts[0]


def test_handle_chat_returns_a_tool_call() -> None:
    runner = FakeRunner('{"action":"tool","name":"read","arguments":{"path":"a.py"}}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    response = gateway.handle_chat(
        {
            "messages": [{"role": "user", "content": "read a.py"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read", "parameters": {"type": "object"}},
                }
            ],
        }
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"] == {"name": "read", "arguments": '{"path": "a.py"}'}
    assert call["id"].startswith("call_gitbot_")


def test_handle_chat_retries_one_invalid_model_answer() -> None:
    runner = FakeRunner("not json", '{"action":"final","content":"repaired"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    response = gateway.handle_chat({"messages": [{"role": "user", "content": "hello"}]})

    assert response["choices"][0]["message"]["content"] == "repaired"
    assert len(runner.prompts) == 2
    assert "previous answer was not" in runner.prompts[1]


def test_handle_chat_can_disable_invalid_json_retry() -> None:
    runner = FakeRunner("not json")
    gateway = AIGateway(GatewayConfig(enabled=True, retry_invalid_json=False), runner=runner)

    with pytest.raises(GatewayError, match="JSON"):
        gateway.handle_chat({"messages": [{"role": "user", "content": "hello"}]})
    assert len(runner.prompts) == 1


def test_handle_chat_retry_does_not_forbid_tools() -> None:
    runner = FakeRunner("not json", '{"action":"tool","name":"read","arguments":{"path":"a"}}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    response = gateway.handle_chat(
        {
            "messages": [{"role": "user", "content": "read a"}],
            "tools": [{"type": "function", "function": {"name": "read"}}],
        }
    )

    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert len(runner.prompts) == 2
    assert "Do not use tools" not in runner.prompts[1]
    assert "may still select one tool" in runner.prompts[1]


@pytest.mark.parametrize(
    ("text", "code", "status", "provider_code"),
    [
        (
            "Rate limit exceeded: free-models-per-day",
            "ai_gateway_rate_limited",
            429,
            "rate_limit",
        ),
        (
            "Model unavailable: openrouter/cohere/north-mini-code:free",
            "ai_gateway_model_unavailable",
            503,
            "no_route",
        ),
        (
            "provider.no-route",
            "ai_gateway_model_unavailable",
            503,
            "no_route",
        ),
        (
            "Free usage limit reached",
            "ai_gateway_rate_limited",
            429,
            "rate_limit",
        ),
        (
            "Invalid API key",
            "ai_gateway_auth_failed",
            502,
            "auth",
        ),
    ],
)
def test_classify_provider_error_maps_stable_codes(
    text: str, code: str, status: int, provider_code: str
) -> None:
    error = _classify_provider_error(text)
    assert error is not None
    assert error.code == code
    assert error.http_status == status
    assert error.provider_code == provider_code
    assert error.details["provider_code"] == provider_code


def test_classify_provider_error_ignores_unrelated_text() -> None:
    assert _classify_provider_error("model returned invalid JSON") is None


def test_extract_host_text_classifies_provider_errors() -> None:
    stdout = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": "Model unavailable: openrouter/cohere/north-mini-code:free",
            },
        }
    )
    with pytest.raises(GatewayProviderError) as caught:
        _extract_host_text(stdout)
    assert caught.value.code == "ai_gateway_model_unavailable"
    assert caught.value.http_status == 503


def test_extract_host_text_redacts_credential_shaped_provider_errors() -> None:
    secret = "token=abcdefghijklmnopqrstuvwxyz"
    stdout = json.dumps({"type": "error", "error": {"message": f"provider said {secret}"}})
    with pytest.raises(GatewayError) as caught:
        _extract_host_text(stdout)
    assert secret not in caught.value.message
    assert "[REDACTED:" in caught.value.message


def test_handle_chat_honours_tool_choice_none_and_forced_function() -> None:
    runner = FakeRunner(
        '{"action":"final","content":"no tools"}',
        '{"action":"tool","name":"write","arguments":{"path":"a"}}',
    )
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    tools = [
        {"type": "function", "function": {"name": "read"}},
        {"type": "function", "function": {"name": "write"}},
    ]

    gateway.handle_chat(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": tools,
            "tool_choice": "none",
        }
    )
    assert "read" not in runner.prompts[0]
    assert "write" not in runner.prompts[0]

    response = gateway.handle_chat(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": "write"}},
        }
    )
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert '"name": "write"' in runner.prompts[1]
    assert '"name": "read"' not in runner.prompts[1]


def test_handle_chat_bounds_output_and_rejects_large_tool_arguments() -> None:
    gateway = AIGateway(
        GatewayConfig(enabled=True, max_output_chars=256),
        runner=FakeRunner(json.dumps({"action": "final", "content": "x" * 1000})),
    )
    response = gateway.handle_chat({"messages": [{"role": "user", "content": "hello"}]})
    assert len(response["choices"][0]["message"]["content"]) == 256

    gateway = AIGateway(
        GatewayConfig(enabled=True, max_output_chars=256),
        runner=FakeRunner(
            json.dumps({"action": "tool", "name": "read", "arguments": {"blob": "x" * 1000}})
        ),
    )
    with pytest.raises(GatewayError, match="too large"):
        gateway.handle_chat(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "function": {"name": "read"}}],
            }
        )


def test_handle_chat_rejects_an_oversized_prompt() -> None:
    gateway = AIGateway(
        GatewayConfig(enabled=True, max_prompt_chars=1024),
        runner=FakeRunner('{"action":"final","content":"never"}'),
    )
    with pytest.raises(GatewayRequestError, match="prompt exceeds"):
        gateway.handle_chat({"messages": [{"role": "user", "content": "x" * 2000}]})


def test_http_endpoints_require_a_valid_capability_token() -> None:
    with _running_gateway('{"action":"final","content":"ok"}') as (gateway, _runner):
        status, payload = _request(gateway, "GET", "/v1/models")
        assert status == 401
        assert payload["error"]["code"] == "unauthorized"

        status, payload = _request(
            gateway,
            "GET",
            "/v1/models",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert status == 401

        status, payload = _request(
            gateway,
            "GET",
            "/v1/models",
            headers={"Authorization": f"Basic {gateway.token}"},
        )
        assert status == 401

        status, payload = _request(gateway, "GET", "/v1/models", headers=_auth(gateway.token))
        assert status == 200
        assert payload["data"][0]["id"] == DEFAULT_GATEWAY_MODEL_ID
        assert payload["data"][0]["owned_by"] == "gitbot"


def test_http_rejects_unknown_routes_and_malformed_requests() -> None:
    with _running_gateway('{"action":"final","content":"ok"}') as (gateway, _runner):
        status, payload = _request(gateway, "GET", "/v1/nope", headers=_auth(gateway.token))
        assert status == 404
        assert payload["error"]["code"] == "not_found"

        status, payload = _request(
            gateway,
            "POST",
            "/v1/nope",
            body=b"{}",
            headers=_auth(gateway.token),
        )
        assert status == 404

        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=b"",
            headers={**_auth(gateway.token), "Transfer-Encoding": "chunked"},
        )
        assert status == 411
        assert payload["error"]["code"] == "length_required"

        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=b"not json",
            headers=_auth(gateway.token),
        )
        assert status == 400
        assert payload["error"]["code"] == "bad_request"

        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=b"[]",
            headers=_auth(gateway.token),
        )
        assert status == 400
        assert "must be an object" in payload["error"]["message"]


def test_http_enforces_the_request_size_limit() -> None:
    with _running_gateway(
        '{"action":"final","content":"ok"}',
        config=GatewayConfig(enabled=True, max_request_bytes=1024),
    ) as (gateway, _runner):
        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=b"x" * 2000,
            headers=_auth(gateway.token),
        )
    assert status == 413
    assert payload["error"]["code"] == "payload_too_large"


def test_http_chat_completion_round_trip_and_streaming() -> None:
    with _running_gateway(
        '{"action":"final","content":"streamed"}',
        '{"action":"final","content":"second"}',
        '{"action":"tool","name":"read","arguments":{"path":"a"}}',
    ) as (gateway, _runner):
        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=_chat_body(),
            headers=_auth(gateway.token),
        )
        assert status == 200
        assert payload["choices"][0]["message"]["content"] == "streamed"

        connection = _UnixHTTPConnection(gateway._socket_path)
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=_chat_body(stream=True),
                headers=_auth(gateway.token),
            )
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type") == "text/event-stream"
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.read().decode("utf-8").splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
        finally:
            connection.close()
        assert events[0]["choices"][0]["delta"]["content"] == "second"
        assert events[-1]["choices"][0]["finish_reason"] == "stop"
        assert events[-1]["usage"]["total_tokens"] >= 1

        connection = _UnixHTTPConnection(gateway._socket_path)
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=_chat_body(
                    stream=True,
                    tools=[{"type": "function", "function": {"name": "read"}}],
                ),
                headers=_auth(gateway.token),
            )
            response = connection.getresponse()
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.read().decode("utf-8").splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
        finally:
            connection.close()
        assert events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "read"
        assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_http_redacts_provider_errors_before_returning_them() -> None:
    secret = "token=abcdefghijklmnopqrstuvwxyz"
    with _running_gateway() as (gateway, runner):
        runner.complete_error = GatewayError(f"provider said {secret}")
        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=_chat_body(),
            headers=_auth(gateway.token),
        )
    assert status == 502
    assert payload["error"]["code"] == "ai_gateway_error"
    assert secret not in payload["error"]["message"]
    assert "[REDACTED:" in payload["error"]["message"]


def test_http_maps_request_errors_to_400() -> None:
    with _running_gateway('{"action":"final","content":"ok"}') as (gateway, _runner):
        status, payload = _request(
            gateway,
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"messages": []}).encode("utf-8"),
            headers=_auth(gateway.token),
        )
    assert status == 400
    assert payload["error"]["code"] == "ai_gateway_bad_request"


def test_gateway_lifecycle_is_idempotent_and_cleans_up() -> None:
    runner = FakeRunner('{"action":"final","content":"ok"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    socket_path = gateway._socket_path

    gateway.start()
    gateway.start()
    assert runner.started == 1
    assert socket_path.is_socket()

    gateway.stop()
    gateway.stop()
    assert runner.stopped == 2
    assert not socket_path.exists()


def test_gateway_start_failure_stops_the_runner() -> None:
    runner = FakeRunner()
    runner.start_error = RuntimeError("boom")
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    with pytest.raises(RuntimeError, match="boom"):
        gateway.start()

    assert runner.stopped == 1
    assert not gateway._socket_path.exists()


def test_gateway_rejects_a_runner_without_a_start_method() -> None:
    gateway = AIGateway(GatewayConfig(enabled=True), runner=object())
    with pytest.raises(GatewayError, match="invalid"):
        gateway.start()


def test_gateway_endpoint_requires_the_relay_mount() -> None:
    policy = GatewayPolicy(
        provider_id=DEFAULT_GATEWAY_ID,
        model_id=DEFAULT_GATEWAY_MODEL_ID,
        base_url="http://127.0.0.1:18080/v1",
        api_key=TOKEN,
    )
    endpoint = GatewayEndpoint(
        policy=policy,
        relay_port=18080,
        ro_binds=(),
        rw_binds=(),
    )
    with pytest.raises(GatewayError, match="relay script is not mounted"):
        endpoint.wrap_command(["python3", "-c", "pass"])


def test_gateway_cancellation_prevents_a_model_call() -> None:
    cancel = threading.Event()
    cancel.set()
    runner = FakeRunner('{"action":"final","content":"never"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner, cancel_event=cancel)

    with pytest.raises(GatewayCancelledError, match="cancelled"):
        gateway.handle_chat({"messages": [{"role": "user", "content": "hello"}]})

    assert runner.prompts == []
    assert runner.started == 0


def test_gateway_endpoint_describes_the_sandbox_contract() -> None:
    runner = FakeRunner()
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    endpoint = gateway.endpoint

    assert endpoint.policy.provider_id == DEFAULT_GATEWAY_ID
    assert endpoint.policy.model_id == DEFAULT_GATEWAY_MODEL_ID
    assert endpoint.policy.base_url == f"http://127.0.0.1:{DEFAULT_RELAY_PORT}/v1"
    assert endpoint.policy.api_key == gateway.token
    assert endpoint.relay_port == DEFAULT_RELAY_PORT
    assert endpoint.ro_binds == ((gateway_module.RELAY_SCRIPT, SANDBOX_RELAY_SCRIPT),)
    assert endpoint.rw_binds == ((gateway._socket_path, SANDBOX_GATEWAY_SOCKET),)

    wrapped = endpoint.wrap_command(["/run/gitbot/bin/opencode", "run"])
    assert wrapped == [
        "python3",
        SANDBOX_RELAY_SCRIPT,
        "--listen",
        f"127.0.0.1:{DEFAULT_RELAY_PORT}",
        "--unix",
        SANDBOX_GATEWAY_SOCKET,
        "--",
        "/run/gitbot/bin/opencode",
        "run",
    ]


def test_prepare_sandbox_command_wraps_argv_and_mounts_the_executable(
    tmp_path: Path,
) -> None:
    policy = GatewayPolicy(
        provider_id=DEFAULT_GATEWAY_ID,
        model_id=DEFAULT_GATEWAY_MODEL_ID,
        base_url="http://127.0.0.1:18080/v1",
        api_key=TOKEN,
    )
    endpoint = GatewayEndpoint(
        policy=policy,
        relay_port=18080,
        ro_binds=((Path("/host/relay.py"), SANDBOX_RELAY_SCRIPT),),
        rw_binds=(),
    )

    argv, ro_binds = prepare_sandbox_command(
        endpoint,
        [sys.executable, "run", "--standalone", "hello"],
        executable=sys.executable,
    )
    target = resolve_sandbox_executable(sys.executable)
    assert argv[0] == "python3"
    assert argv[1] == SANDBOX_RELAY_SCRIPT
    assert argv[2:6] == ["--listen", "127.0.0.1:18080", "--unix", SANDBOX_GATEWAY_SOCKET]
    assert argv[6] == "--"
    assert argv[7] == target
    assert argv[-1] == "hello"
    assert ro_binds == endpoint.ro_binds

    executable = tmp_path / "outside-opencode"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    argv, ro_binds = prepare_sandbox_command(
        endpoint,
        [str(executable), "run"],
        executable=str(executable),
    )
    assert argv[-2] == "/run/gitbot/bin/outside-opencode"
    assert ro_binds[-1] == (executable.resolve(), "/run/gitbot/bin/outside-opencode")

    with pytest.raises(GatewayError, match="empty"):
        prepare_sandbox_command(endpoint, [], executable=sys.executable)


def test_resolve_sandbox_executable_uses_runtime_roots_or_the_exec_directory(
    tmp_path: Path,
) -> None:
    assert resolve_sandbox_executable(sys.executable) == str(Path(sys.executable).resolve())

    executable = tmp_path / "custom-opencode"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    assert resolve_sandbox_executable(str(executable)) == "/run/gitbot/bin/custom-opencode"


def test_ai_gateway_session_is_a_noop_when_disabled() -> None:
    with ai_gateway_session(None) as endpoint:
        assert endpoint is None
    with ai_gateway_session(GatewayConfig(enabled=False)) as endpoint:
        assert endpoint is None


def test_ai_gateway_session_starts_and_stops_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner('{"action":"final","content":"ok"}')
    monkeypatch.setattr(gateway_module, "HostModelRunner", lambda _config: runner)

    with ai_gateway_session(
        GatewayConfig(enabled=True, sandbox_bwrap="bwrap"),
        bwrap="custom-bwrap",
    ) as endpoint:
        assert endpoint is not None
        assert endpoint.policy.api_key
        assert runner.started == 1

    assert runner.stopped == 1
    assert not endpoint.rw_binds[0][0].exists()


def test_ai_gateway_session_stops_the_gateway_after_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(gateway_module, "HostModelRunner", lambda _config: runner)

    with (
        pytest.raises(RuntimeError, match="task failed"),
        ai_gateway_session(GatewayConfig(enabled=True)),
    ):
        raise RuntimeError("task failed")

    assert runner.stopped == 1


def test_host_tunnel_allows_only_opencode_hosts() -> None:
    tunnel = HostTunnel()
    try:
        assert tunnel._allowed("opencode.ai")
        assert tunnel._allowed("API.OPENCODE.AI")
        assert tunnel._allowed("api.opencode.ai.")
        assert not tunnel._allowed("opencode.ai.evil.example")
        assert not tunnel._allowed("evilopencode.ai")
        assert not tunnel._allowed("evil.example")
        assert not tunnel._allowed("")
    finally:
        tunnel.stop()

    custom = HostTunnel(allowed_suffixes=(".example.test",))
    try:
        assert custom._allowed("api.example.test")
        assert not custom._allowed("api.opencode.ai")
    finally:
        custom.stop()


def test_host_tunnel_rejects_non_connect_and_forbidden_hosts() -> None:
    tunnel = HostTunnel()
    try:
        client, server = socket.socketpair()
        try:
            client.sendall(b"GET / HTTP/1.1\r\nHost: opencode.ai\r\n\r\n")
            tunnel._handle(server)
            assert client.recv(1024).startswith(b"HTTP/1.1 405")
        finally:
            client.close()

        client, server = socket.socketpair()
        try:
            client.sendall(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
            tunnel._handle(server)
            assert client.recv(1024).startswith(b"HTTP/1.1 403")
        finally:
            client.close()

        client, server = socket.socketpair()
        try:
            client.sendall(b"CONNECT opencode.ai:8443 HTTP/1.1\r\n\r\n")
            tunnel._handle(server)
            assert client.recv(1024).startswith(b"HTTP/1.1 403")
        finally:
            client.close()
    finally:
        tunnel.stop()


def test_host_model_runner_requires_an_executable_and_a_started_lifecycle() -> None:
    with pytest.raises(GatewayError, match="not found"):
        HostModelRunner(GatewayConfig(executable="definitely-missing-opencode"))

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    try:
        with pytest.raises(GatewayError, match="not started"):
            runner.complete("hello")
    finally:
        runner.stop()


def test_host_model_runner_builds_a_network_restricted_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeSpec:
        pass

    def fake_build(*args: Any, **kwargs: Any) -> FakeSpec:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeSpec()

    def fake_run(spec: Any, **kwargs: Any) -> SandboxRunResult:
        captured["spec"] = spec
        captured["run_kwargs"] = kwargs
        return SandboxRunResult(
            exit_code=0,
            stdout=json.dumps({"type": "text", "part": {"text": "model answer"}}),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            cancelled=False,
            duration_ms=1,
        )

    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", fake_build)
    monkeypatch.setattr(gateway_module, "run_sandbox_command", fake_run)

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        assert runner.complete("hello model") == "model answer"
    finally:
        runner.stop()

    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == "/repo"
    assert kwargs["extra_env"]["HTTPS_PROXY"] == f"http://127.0.0.1:{DEFAULT_HOST_RELAY_PORT}"
    assert kwargs["extra_env"]["NO_PROXY"] == ""
    assert (runner._tunnel.socket_path, SANDBOX_UPSTREAM_SOCKET) in kwargs["extra_rw_binds"]
    assert (gateway_module.RELAY_SCRIPT, SANDBOX_RELAY_SCRIPT) in kwargs["extra_ro_binds"]
    wrapper = captured["args"][2]
    assert wrapper[:2] == ["python3", SANDBOX_RELAY_SCRIPT]
    assert "--unix" in wrapper
    assert SANDBOX_UPSTREAM_SOCKET in wrapper
    assert "hello model" in wrapper
    assert "--agent" in wrapper
    assert wrapper[wrapper.index("--agent") + 1] == DEFAULT_GATEWAY_ID


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            SandboxRunResult(None, "", "", False, False, False, True, 1),
            "cancelled",
        ),
        (
            SandboxRunResult(None, "", "", False, False, True, False, 1),
            "timed out",
        ),
        (
            SandboxRunResult(
                1,
                "",
                "token=abcdefghijklmnopqrstuvwxyz",
                False,
                False,
                False,
                False,
                1,
            ),
            "exited with 1",
        ),
    ],
)
def test_host_model_runner_maps_failures_to_gateway_errors(
    monkeypatch: pytest.MonkeyPatch,
    result: SandboxRunResult,
    message: str,
) -> None:
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", lambda *_a, **_k: object())
    monkeypatch.setattr(gateway_module, "run_sandbox_command", lambda *_a, **_k: result)

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        with pytest.raises(GatewayError, match=message) as caught:
            runner.complete("hello")
    finally:
        runner.stop()

    assert "abcdefghijklmnopqrstuvwxyz" not in caught.value.message


def test_host_model_runner_maps_stdout_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", lambda *_a, **_k: object())
    stdout = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": "Model unavailable: openrouter/cohere/north-mini-code:free",
            },
        }
    )
    monkeypatch.setattr(
        gateway_module,
        "run_sandbox_command",
        lambda *_a, **_k: SandboxRunResult(1, stdout, "", False, False, False, False, 1),
    )

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        with pytest.raises(GatewayProviderError) as caught:
            runner.complete("hello")
    finally:
        runner.stop()

    assert caught.value.code == "ai_gateway_model_unavailable"
    assert caught.value.http_status == 503
    assert caught.value.provider_code == "no_route"


def test_host_model_runner_maps_timeout_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", lambda *_a, **_k: object())

    monkeypatch.setattr(
        gateway_module,
        "run_sandbox_command",
        lambda *_a, **_k: SandboxRunResult(None, "", "", False, False, True, False, 1),
    )
    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        with pytest.raises(GatewayTimeoutError, match="timed out"):
            runner.complete("hello")
    finally:
        runner.stop()

    cancel = threading.Event()
    cancel.set()
    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        with pytest.raises(GatewayCancelledError, match="cancelled"):
            runner.complete("hello", cancel_event=cancel)
    finally:
        runner.stop()


def test_host_model_runner_requires_the_relay_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    missing = Path("/definitely/missing/gitbot-relay.py")
    monkeypatch.setattr(gateway_module, "RELAY_SCRIPT", missing)

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    with pytest.raises(GatewayError, match="relay script is missing"):
        runner.start()
    runner.stop()


def test_gateway_policy_never_reveals_its_token() -> None:
    policy = GatewayPolicy(
        provider_id=DEFAULT_GATEWAY_ID,
        model_id=DEFAULT_GATEWAY_MODEL_ID,
        base_url="http://127.0.0.1:18080/v1",
        api_key=TOKEN,
    )
    assert TOKEN not in repr(policy)
    assert policy.api_key == TOKEN

    with pytest.raises(PolicyViolation, match="too short"):
        GatewayPolicy(
            provider_id=DEFAULT_GATEWAY_ID,
            model_id=DEFAULT_GATEWAY_MODEL_ID,
            base_url="http://127.0.0.1:18080/v1",
            api_key="short",
        ).validate()

    for base_url in (
        "https://127.0.0.1:18080/v1",
        "http://localhost:18080/v1",
        "http://127.0.0.1:18080/v2",
        "http://127.0.0.1:18080/v1?x=1",
        "http://user:pass@127.0.0.1:18080/v1",
    ):
        with pytest.raises(PolicyViolation, match="base URL"):
            GatewayPolicy(
                provider_id=DEFAULT_GATEWAY_ID,
                model_id=DEFAULT_GATEWAY_MODEL_ID,
                base_url=base_url,
                api_key=TOKEN,
            ).validate()


# --- Host tool rejection (fix A) -------------------------------------------


def test_host_used_tools_detects_any_host_tool_event() -> None:
    assert _host_used_tools("") is False
    assert (
        _host_used_tools(json.dumps({"type": "text", "part": {"type": "text", "text": "hi"}}))
        is False
    )
    assert (
        _host_used_tools(json.dumps({"type": "step_finish", "part": {"type": "step-finish"}}))
        is False
    )
    assert _host_used_tools(json.dumps({"type": "tool_use", "part": {"type": "tool"}})) is True
    assert _host_used_tools(json.dumps({"type": "tool", "part": {}})) is True
    assert _host_used_tools(json.dumps({"type": "step_finish", "part": {"type": "tool"}})) is True


def test_host_model_runner_rejects_host_side_tool_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", lambda *_a, **_k: object())
    secret_path = "/home/victim/.ssh/id_ed25519"
    secret_output = "PRIVATE-KEY-MATERIAL"
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "input": {"path": secret_path},
                            "output": secret_output,
                        },
                    },
                }
            ),
            json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}),
        ]
    )
    monkeypatch.setattr(
        gateway_module,
        "run_sandbox_command",
        lambda *_a, **_k: SandboxRunResult(0, stdout, "", False, False, False, False, 1),
    )

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        with pytest.raises(GatewayProtocolError) as caught:
            runner.complete("hello")
    finally:
        runner.stop()

    assert caught.value.code == "ai_gateway_protocol_error"
    assert caught.value.http_status == 502
    message = caught.value.message
    assert secret_output not in message
    assert secret_path not in message
    assert "id_ed25519" not in message
    assert ".ssh" not in message


def test_host_model_runner_accepts_a_compliant_text_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", lambda *_a, **_k: object())
    stdout = json.dumps({"type": "text", "part": {"type": "text", "text": "model answer"}})
    monkeypatch.setattr(
        gateway_module,
        "run_sandbox_command",
        lambda *_a, **_k: SandboxRunResult(0, stdout, "", False, False, False, False, 1),
    )

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        assert runner.complete("hello") == "model answer"
    finally:
        runner.stop()


# --- Host agent configuration (fixes C1/C2) --------------------------------


def test_host_agent_config_denies_every_host_tool() -> None:
    config = GatewayConfig(model="opencode/test-model")
    document = _host_agent_config(config)
    agent = document["agents"][DEFAULT_GATEWAY_ID]

    denied = {rule["action"] for rule in agent["permissions"]}
    assert denied == {
        "read",
        "glob",
        "grep",
        "list",
        "shell",
        "write",
        "execute",
        "edit",
        "browser",
        "task",
        "webfetch",
        "websearch",
    }
    assert all(
        rule == {"action": rule["action"], "resource": "*", "effect": "deny"}
        for rule in agent["permissions"]
    )
    # The verifier accepts the generated document unchanged.
    verify_host_agent_config(document, config)


def test_host_agent_config_never_contains_credentials_or_host_paths() -> None:
    document = _host_agent_config(GatewayConfig(model="opencode/test-model"))
    serialized = json.dumps(document)
    assert "apiKey" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized.lower()
    assert ".ssh" not in serialized
    assert "opencode.db" not in serialized


def _mutate_model(document: dict[str, Any]) -> None:
    document["model"] = "opencode/some-other-model"


def _mutate_no_agents(document: dict[str, Any]) -> None:
    document["agents"] = {}


def _mutate_extra_agent(document: dict[str, Any]) -> None:
    document["agents"]["sneaky"] = {"mode": "primary"}


def _mutate_drop_read_deny(document: dict[str, Any]) -> None:
    document["agents"][DEFAULT_GATEWAY_ID]["permissions"] = [
        rule
        for rule in document["agents"][DEFAULT_GATEWAY_ID]["permissions"]
        if rule["action"] != "read"
    ]


def _mutate_read_allow(document: dict[str, Any]) -> None:
    document["agents"][DEFAULT_GATEWAY_ID]["permissions"][0] = {
        "action": "read",
        "resource": "*",
        "effect": "allow",
    }


def _mutate_credential_key(document: dict[str, Any]) -> None:
    document["agents"][DEFAULT_GATEWAY_ID]["apiKey"] = "sk-secret"


def _mutate_unknown_top_level(document: dict[str, Any]) -> None:
    document["provider"] = {"evil": {}}


def _mutate_bad_mode(document: dict[str, Any]) -> None:
    document["agents"][DEFAULT_GATEWAY_ID]["mode"] = "subagent"


def _mutate_bad_steps(document: dict[str, Any]) -> None:
    document["agents"][DEFAULT_GATEWAY_ID]["steps"] = 0


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_model,
        _mutate_no_agents,
        _mutate_extra_agent,
        _mutate_drop_read_deny,
        _mutate_read_allow,
        _mutate_credential_key,
        _mutate_unknown_top_level,
        _mutate_bad_mode,
        _mutate_bad_steps,
    ],
    ids=lambda fn: fn.__name__,
)
def test_verify_host_agent_config_fails_closed_on_tampering(mutate: Any) -> None:
    config = GatewayConfig(model="opencode/test-model")
    document = _host_agent_config(config)
    mutate(document)
    with pytest.raises(GatewayError):
        verify_host_agent_config(document, config)


def test_verify_host_agent_config_rejects_non_objects() -> None:
    config = GatewayConfig(model="opencode/test-model")
    for bad in (None, [], "opencode.json", 5):
        with pytest.raises(GatewayError):
            verify_host_agent_config(bad, config)


def test_host_model_runner_verifies_the_written_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tampered document is rejected before any model process is started."""
    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", lambda *_a, **_k: object())

    def fail_run(*_args: Any, **_kwargs: Any) -> SandboxRunResult:  # pragma: no cover
        raise AssertionError("the model must not run when verification fails")

    monkeypatch.setattr(gateway_module, "run_sandbox_command", fail_run)

    def tampered(config: GatewayConfig) -> dict[str, Any]:
        return {"$schema": "https://opencode.ai/config.json", "model": config.model}

    monkeypatch.setattr(gateway_module, "_host_agent_config", tampered)

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        with pytest.raises(GatewayError, match="exactly one agent"):
            runner.complete("hello")
    finally:
        runner.stop()


def test_host_model_runner_sandbox_mounts_no_host_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_build(*_args: Any, **kwargs: Any) -> object:
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(gateway_module, "require_sandbox", lambda **_kwargs: None)
    monkeypatch.setattr(gateway_module, "build_sandbox_command", fake_build)
    monkeypatch.setattr(
        gateway_module,
        "run_sandbox_command",
        lambda *_a, **_k: SandboxRunResult(
            0,
            json.dumps({"type": "text", "part": {"type": "text", "text": "ok"}}),
            "",
            False,
            False,
            False,
            False,
            1,
        ),
    )

    runner = HostModelRunner(GatewayConfig(executable=sys.executable))
    runner.start()
    try:
        assert runner.complete("hello") == "ok"
    finally:
        runner.stop()

    mounts = list(captured["kwargs"]["extra_ro_binds"]) + list(captured["kwargs"]["extra_rw_binds"])
    for source, _target in mounts:
        rendered = str(source)
        assert ".ssh" not in rendered
        assert "opencode.db" not in rendered
        assert ".config/opencode" not in rendered
        assert ".local/share/opencode" not in rendered


# --- Repository evidence rule (fix B) --------------------------------------


def test_has_tool_result_requires_a_linked_tool_message() -> None:
    assert _has_tool_result([]) is False
    assert _has_tool_result([{"role": "user", "content": "hi"}]) is False
    assert _has_tool_result([{"role": "assistant", "content": "hi"}]) is False
    assert _has_tool_result([{"role": "tool", "content": "x"}]) is False
    assert _has_tool_result([{"role": "tool", "content": "x", "tool_call_id": ""}]) is False
    # An orphaned tool message is not evidence of a real tool round trip.
    assert _has_tool_result([{"role": "tool", "content": "x", "tool_call_id": "call_1"}]) is False

    call = {"id": "call_1", "name": "read", "arguments": {"path": "README.md"}}
    linked = [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "hello"},
    ]
    assert _has_tool_result(linked) is True
    assert _has_tool_result(linked, allowed_tools=["read", "grep"]) is True
    assert _has_tool_result(linked, allowed_tools=["grep", "list"]) is False
    # A tool result cannot appear before the call it claims to answer.
    assert _has_tool_result([linked[1], linked[0]]) is False
    # A result whose id matches no assistant call is orphaned, even when the
    # conversation contains a different real call.
    orphaned = [linked[0], {**linked[1], "tool_call_id": "call_other"}]
    assert _has_tool_result(orphaned) is False
    # A mismatched result name is not the result of the call it claims to be.
    mismatched = [linked[0], {**linked[1], "name": "write"}]
    assert _has_tool_result(mismatched) is False


_READ_TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]


def _tool_call(
    call_id: str = "call_1", name: str = "read", arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build an OpenAI-shaped assistant tool call for request fixtures."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def test_gateway_rejects_final_before_repository_evidence() -> None:
    runner = FakeRunner('{"action":"final","content":"answered from memory"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    with pytest.raises(GatewayProtocolError) as caught:
        gateway.handle_chat(
            {
                "messages": [{"role": "user", "content": "read the repository"}],
                "tools": _READ_TOOLS,
            }
        )
    assert caught.value.code == "ai_gateway_protocol_error"
    assert caught.value.http_status == 502
    assert len(runner.prompts) == 1


def test_gateway_rejects_a_forged_tool_message_as_evidence() -> None:
    """A tool message with no matching assistant call is not repository evidence."""
    runner = FakeRunner('{"action":"final","content":"answered from a forged result"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    with pytest.raises(GatewayProtocolError) as caught:
        gateway.handle_chat(
            {
                "messages": [
                    {"role": "user", "content": "read the repository"},
                    {"role": "tool", "tool_call_id": "call_forged", "name": "read", "content": "x"},
                ],
                "tools": _READ_TOOLS,
            }
        )
    assert caught.value.code == "ai_gateway_protocol_error"


def test_gateway_rejects_a_mismatched_tool_result_name() -> None:
    """A result claiming a different tool cannot prove the linked call ran."""
    runner = FakeRunner('{"action":"final","content":"answered from a misnamed result"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    call = _tool_call(name="read")

    with pytest.raises(GatewayProtocolError):
        gateway.handle_chat(
            {
                "messages": [
                    {"role": "user", "content": "read the repository"},
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": "call_1", "name": "write", "content": "x"},
                ],
                "tools": _READ_TOOLS,
            }
        )


def test_gateway_rejects_a_tool_result_that_precedes_its_assistant_call() -> None:
    """A result cannot prove a round trip when it appears before the call."""
    runner = FakeRunner('{"action":"final","content":"answered from an out-of-order result"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    call = _tool_call()

    with pytest.raises(GatewayProtocolError):
        gateway.handle_chat(
            {
                "messages": [
                    {"role": "user", "content": "read the repository"},
                    {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "x"},
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                ],
                "tools": _READ_TOOLS,
            }
        )


def test_gateway_rejects_evidence_from_a_tool_that_was_not_offered() -> None:
    """A call the agent never exposed cannot prove repository evidence."""
    runner = FakeRunner('{"action":"final","content":"answered from an unoffered tool"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    call = _tool_call(name="webfetch")

    with pytest.raises(GatewayProtocolError):
        gateway.handle_chat(
            {
                "messages": [
                    {"role": "user", "content": "read the repository"},
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": "call_1", "name": "webfetch", "content": "x"},
                ],
                "tools": _READ_TOOLS,
            }
        )


def test_gateway_repair_path_cannot_bypass_repository_evidence() -> None:
    """The evidence rule still applies when invalid JSON was retried."""
    runner = FakeRunner("not json", '{"action":"final","content":"answered after repair"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    with pytest.raises(GatewayProtocolError):
        gateway.handle_chat(
            {
                "messages": [{"role": "user", "content": "read the repository"}],
                "tools": _READ_TOOLS,
            }
        )
    assert len(runner.prompts) == 2


def test_gateway_repair_path_accepts_final_after_repository_evidence() -> None:
    """A repaired final answer is accepted when the evidence already exists."""
    runner = FakeRunner("not json", '{"action":"final","content":"README says hello"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    call = _tool_call(arguments={"path": "README.md"})

    response = gateway.handle_chat(
        {
            "messages": [
                {"role": "user", "content": "read README"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "hello"},
            ],
            "tools": _READ_TOOLS,
        }
    )
    assert response["choices"][0]["message"]["content"] == "README says hello"
    assert len(runner.prompts) == 2


def test_gateway_accepts_final_after_a_repository_tool_round_trip() -> None:
    runner = FakeRunner(
        '{"action":"tool","name":"read","arguments":{"path":"README.md"}}',
        '{"action":"final","content":"README says hello"}',
    )
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)

    first = gateway.handle_chat(
        {"messages": [{"role": "user", "content": "read README"}], "tools": _READ_TOOLS}
    )
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert first["choices"][0]["finish_reason"] == "tool_calls"

    second = gateway.handle_chat(
        {
            "messages": [
                {"role": "user", "content": "read README"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "name": "read", "content": "hello"},
            ],
            "tools": _READ_TOOLS,
        }
    )
    assert second["choices"][0]["message"]["content"] == "README says hello"
    assert second["choices"][0]["finish_reason"] == "stop"


def test_gateway_allows_a_final_when_no_tools_are_offered() -> None:
    runner = FakeRunner('{"action":"final","content":"plain answer"}')
    gateway = AIGateway(GatewayConfig(enabled=True), runner=runner)
    response = gateway.handle_chat({"messages": [{"role": "user", "content": "hello"}]})
    assert response["choices"][0]["message"]["content"] == "plain answer"


def test_gateway_rejects_an_unknown_tool_name() -> None:
    gateway = AIGateway(
        GatewayConfig(enabled=True, retry_invalid_json=False),
        runner=FakeRunner('{"action":"tool","name":"evil","arguments":{}}'),
    )
    with pytest.raises(GatewayError, match="unknown tool"):
        gateway.handle_chat({"messages": [{"role": "user", "content": "x"}], "tools": _READ_TOOLS})


def test_gateway_rejects_malformed_tool_arguments() -> None:
    gateway = AIGateway(
        GatewayConfig(enabled=True, retry_invalid_json=False),
        runner=FakeRunner('{"action":"tool","name":"read","arguments":"not-json"}'),
    )
    with pytest.raises(GatewayError, match="invalid tool arguments"):
        gateway.handle_chat({"messages": [{"role": "user", "content": "x"}], "tools": _READ_TOOLS})
