"""Tests for OpenCode NDJSON parser."""

from __future__ import annotations

from gitbot.opencode.parser import OpenCodeEventParser


def test_parse_success_events() -> None:
    stdout = (
        '{"type":"step_start","timestamp":1,"sessionID":"s1","part":{"type":"step-start"}}\n'
        '{"type":"tool_use","timestamp":2,"sessionID":"s1","part":{"type":"tool","tool":"shell",'
        '"state":{"status":"completed","input":{"command":"git status"},"output":"ok"}}}\n'
        '{"type":"step_finish","timestamp":3,"sessionID":"s1","part":{"type":"step-finish",'
        '"reason":"tool-calls"}}\n'
        '{"type":"text","timestamp":4,"sessionID":"s1","part":{"type":"text","text":"done"}}'
    ).strip()
    parser = OpenCodeEventParser()
    events = parser.parse_stream(stdout)
    assert len(events) == 4
    assert parser.has_final_text is True
    assert parser.permission_denied is False


def test_parse_resource_permission_denied() -> None:
    stdout = (
        '{"type":"tool_use","timestamp":1,"sessionID":"s1","part":{"type":"tool","tool":"shell",'
        '"state":{"status":"error","input":{"command":"git status"},'
        '"error":"Permission denied: shell"}}}\n'
        '{"type":"text","timestamp":2,"sessionID":"s1","part":{"type":"text","text":"denied"}}'
    ).strip()
    parser = OpenCodeEventParser()
    parser.parse_stream(stdout)
    assert parser.permission_denied is True
    assert len(parser.denied_actions) == 1


def test_parse_top_level_error() -> None:
    stdout = (
        '{"type":"error","timestamp":1,"sessionID":"s1","error":'
        '{"type":"provider.no-route","message":"Model unavailable"}}'
    )
    parser = OpenCodeEventParser()
    parser.parse_stream(stdout)
    assert parser.top_level_error is not None
    assert parser.top_level_error["type"] == "provider.no-route"


def test_strip_ansi() -> None:
    text = "\x1b[31mhello\x1b[0m"
    assert OpenCodeEventParser.strip_ansi(text) == "hello"
