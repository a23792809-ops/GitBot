from __future__ import annotations

import io
import json
import threading
import urllib.error
from unittest.mock import patch

import pytest

from gitbot.opencode.gateway import (
    AIGateway,
    GatewayCancelledError,
    GatewayConfig,
    GatewayError,
    GatewayProviderError,
    GatewayTimeoutError,
    GroqHostRunner,
)


def _runner(monkeypatch, model="groq/openai/gpt-oss-20b"):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    return GroqHostRunner(GatewayConfig(provider="groq", model=model, timeout_s=5.0))


def _fake_response(body):
    return type(
        "Resp",
        (),
        {
            "read": lambda self, n: body,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        },
    )()


def test_groq_success(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps({"choices": [{"message": {"content": "GITBOT_OK"}}]}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        assert runner.complete("hello") == "GITBOT_OK"


def test_groq_exact_outgoing_model_field(monkeypatch):
    runner = _runner(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["data"] = request.data
        return _fake_response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        runner.complete("hello")
    payload = json.loads(captured["data"])
    assert payload["model"] == "openai/gpt-oss-20b"


def test_groq_missing_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    runner = GroqHostRunner(
        GatewayConfig(
            provider="groq",
            model="groq/openai/gpt-oss-20b",
            timeout_s=5.0,
            credential_file="/tmp/opencode/no-such-groq-key",
        )
    )
    with pytest.raises(GatewayProviderError) as exc:
        runner.complete("hello")
    assert exc.value.provider_code == "auth"


def test_groq_rate_limit(monkeypatch):
    runner = _runner(monkeypatch)
    err = urllib.error.HTTPError("url", 429, "rate", {}, io.BytesIO(b'{"error":"rate limit"}'))
    with (
        patch("urllib.request.urlopen", side_effect=err),
        pytest.raises(GatewayProviderError) as exc,
    ):
        runner.complete("hello")
    assert exc.value.provider_code == "rate_limit"


def test_groq_auth_failure(monkeypatch):
    runner = _runner(monkeypatch)
    err = urllib.error.HTTPError("url", 401, "auth", {}, io.BytesIO(b'{"error":"invalid api key"}'))
    with (
        patch("urllib.request.urlopen", side_effect=err),
        pytest.raises(GatewayProviderError) as exc,
    ):
        runner.complete("hello")
    assert exc.value.provider_code == "auth"


def test_groq_timeout(monkeypatch):
    runner = _runner(monkeypatch)
    with (
        patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        pytest.raises(GatewayTimeoutError),
    ):
        runner.complete("hello")


def test_groq_malformed_response(monkeypatch):
    runner = _runner(monkeypatch)
    with (
        patch("urllib.request.urlopen", return_value=_fake_response(b"not-json")),
        pytest.raises(GatewayError),
    ):
        runner.complete("hello")


def test_groq_oversized_response(monkeypatch):
    runner = _runner(monkeypatch)
    import gitbot.opencode.gateway as g

    old = g._MAX_UPSTREAM_BYTES
    g._MAX_UPSTREAM_BYTES = 10
    try:
        with (
            patch("urllib.request.urlopen", return_value=_fake_response(b"x" * 100)),
            pytest.raises(GatewayError),
        ):
            runner.complete("hello")
    finally:
        g._MAX_UPSTREAM_BYTES = old


def test_groq_secret_redaction(monkeypatch):
    runner = _runner(monkeypatch)
    secret = "gsk_" + "a" * 40
    body = json.dumps({"choices": [{"message": {"content": f"leaked {secret}"}}]}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        out = runner.complete("hello")
    assert secret not in out


def test_groq_cancelled(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    ev = threading.Event()
    ev.set()
    runner = GroqHostRunner(
        GatewayConfig(provider="groq", model="groq/openai/gpt-oss-20b", timeout_s=5.0)
    )
    with pytest.raises(GatewayCancelledError):
        runner.complete("hello", cancel_event=ev)


def test_groq_no_retry_on_rate_limit(monkeypatch):
    runner = _runner(monkeypatch)
    err = urllib.error.HTTPError("url", 429, "rate", {}, io.BytesIO(b'{"error":"rate limit"}'))
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise err

    with patch("urllib.request.urlopen", side_effect=boom), pytest.raises(GatewayProviderError):
        runner.complete("hello")
    assert calls["n"] == 1


def test_groq_provider_error_hides_key(monkeypatch):
    runner = _runner(monkeypatch)
    secret = "gsk_" + "a" * 40
    err = urllib.error.HTTPError(
        "url", 401, "auth", {}, io.BytesIO(("invalid key " + secret).encode())
    )
    with (
        patch("urllib.request.urlopen", side_effect=err),
        pytest.raises(GatewayProviderError) as exc,
    ):
        runner.complete("hello")
    assert secret not in str(exc.value)


def test_groq_tool_calls_parsing(monkeypatch):
    runner = _runner(monkeypatch)
    decision = {"action": "tool", "name": "read_file", "arguments": {"path": "README.md"}}
    body = json.dumps({"choices": [{"message": {"content": json.dumps(decision)}}]}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        raw = runner.complete("hello")
    from gitbot.opencode.gateway import parse_gateway_decision

    parsed = parse_gateway_decision(raw, allowed_tools=["read_file"])
    assert parsed.action == "tool"
    assert parsed.name == "read_file"
    assert parsed.arguments == {"path": "README.md"}


def test_groq_structured_json_output(monkeypatch):
    runner = _runner(monkeypatch)
    content = {"summary": "ok", "count": 2}
    body = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        raw = runner.complete("hello")
    import json as _json

    assert _json.loads(raw) == content


def test_groq_malformed_tool_arguments_rejected(monkeypatch):
    runner = _runner(monkeypatch)
    decision = {"action": "tool", "name": "read_file", "arguments": "not-a-dict"}
    body = json.dumps({"choices": [{"message": {"content": json.dumps(decision)}}]}).encode()
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        raw = runner.complete("hello")
    from gitbot.opencode.gateway import parse_gateway_decision

    with pytest.raises(GatewayError):
        parse_gateway_decision(raw, allowed_tools=["read_file"])


def test_groq_gateway_tool_call_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    fake_runner = type(
        "R",
        (),
        {
            "start": lambda self: None,
            "stop": lambda self: None,
            "complete": lambda self, prompt, tools=None, cancel_event=None: (
                '{"action":"tool","name":"read_file","arguments":{"path":"README.md"}}'
            ),
        },
    )()
    cfg = GatewayConfig(provider="groq", model="groq/openai/gpt-oss-20b", timeout_s=5.0)
    gw = AIGateway(cfg, runner=fake_runner)
    gw.start()
    try:
        resp = gw.handle_chat(
            {
                "messages": [{"role": "user", "content": "x"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            }
        )
        assert resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    finally:
        gw.stop()


def test_openrouter_default_provider_unchanged():
    cfg = GatewayConfig()
    assert cfg.provider == "opencode"
    assert cfg.model == "opencode/inkling-2-small-free"


def test_groq_native_tool_call_conversion(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        raw = runner.complete("hello", tools=tools)
    import json as _json

    assert _json.loads(raw) == {
        "action": "tool",
        "name": "read_file",
        "arguments": {"path": "README.md"},
    }


def test_groq_native_tool_call_dict_arguments(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "read_file",
                                    "arguments": {"path": "README.md"},
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        raw = runner.complete("hello", tools=tools)
    import json as _json

    assert _json.loads(raw)["arguments"] == {"path": "README.md"}


def test_groq_native_tool_call_malformed_arguments(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "read_file",
                                    "arguments": "{not-json",
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with (
        patch("urllib.request.urlopen", return_value=_fake_response(body)),
        pytest.raises(GatewayError),
    ):
        runner.complete("hello", tools=tools)


def test_groq_native_tool_call_unknown_tool(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "delete_file",
                                    "arguments": "{}",
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with (
        patch("urllib.request.urlopen", return_value=_fake_response(body)),
        pytest.raises(GatewayError),
    ):
        runner.complete("hello", tools=tools)


def test_groq_native_tool_call_missing_name(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "arguments": "{}",
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with (
        patch("urllib.request.urlopen", return_value=_fake_response(body)),
        pytest.raises(GatewayError),
    ):
        runner.complete("hello", tools=tools)


def test_groq_native_tool_call_multiple(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": "{}"}},
                            {"function": {"name": "read_file", "arguments": "{}"}},
                        ]
                    }
                }
            ]
        }
    ).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with (
        patch("urllib.request.urlopen", return_value=_fake_response(body)),
        pytest.raises(GatewayError),
    ):
        runner.complete("hello", tools=tools)


def test_groq_native_tool_call_invalid_response(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps({"choices": [{"message": "not-a-dict"}]}).encode()
    with (
        patch("urllib.request.urlopen", return_value=_fake_response(body)),
        pytest.raises(GatewayError),
    ):
        runner.complete("hello")


def test_groq_final_text_preserved_with_tools(monkeypatch):
    runner = _runner(monkeypatch)
    body = json.dumps({"choices": [{"message": {"content": "final answer"}}]}).encode()
    tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    with patch("urllib.request.urlopen", return_value=_fake_response(body)):
        assert runner.complete("hello", tools=tools) == "final answer"
