"""NDJSON parser for OpenCode `run --format json` output.

Based on Phase 3 spike findings: parse strictly per line, treat non-JSON as
protocol failure when exit non-zero, strip ANSI, never trust exit 0 alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class EventType(StrEnum):
    STEP_START = "step_start"
    TOOL_USE = "tool_use"
    STEP_FINISH = "step_finish"
    TEXT = "text"
    ERROR = "error"
    UNKNOWN = "unknown"


class ToolState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class NdjsonEvent:
    type: EventType
    raw: dict[str, Any]
    session_id: str | None = None
    timestamp: int | None = None
    part: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(slots=True)
class OpenCodeEventParser:
    events: list[NdjsonEvent] = field(default_factory=list)
    tool_events: list[NdjsonEvent] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    step_starts: int = 0
    has_final_text: bool = False
    permission_denied: bool = False
    denied_actions: list[dict[str, Any]] = field(default_factory=list)
    top_level_error: dict[str, Any] | None = None

    @staticmethod
    def strip_ansi(text: str) -> str:
        return _ANSI_RE.sub("", text)

    def parse_line(self, line: str) -> NdjsonEvent | None:
        line = line.strip()
        if not line:
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None

        etype_str = raw.get("type")
        try:
            etype = (
                EventType(etype_str)
                if etype_str in EventType.__members__.values()
                else EventType.UNKNOWN
            )
        except Exception:
            etype = EventType.UNKNOWN

        part = raw.get("part") if isinstance(raw.get("part"), dict) else None
        error = raw.get("error") if isinstance(raw.get("error"), dict) else None
        session_id = raw.get("sessionID") if isinstance(raw.get("sessionID"), str) else None
        ts = raw.get("timestamp") if isinstance(raw.get("timestamp"), int) else None

        ev = NdjsonEvent(
            type=etype,
            raw=raw,
            session_id=session_id,
            timestamp=ts,
            part=part,
            error=error,
        )
        self._classify(ev)
        self.events.append(ev)
        return ev

    def _classify(self, ev: NdjsonEvent) -> None:
        if ev.type == EventType.STEP_START:
            self.step_starts += 1
            return
        if ev.type == EventType.TEXT and ev.part and isinstance(ev.part.get("text"), str):
            self.text_parts.append(ev.part["text"])
            self.has_final_text = True
            return
        if ev.type == EventType.TOOL_USE and ev.part:
            self.tool_events.append(ev)
            state = ev.part.get("state") if isinstance(ev.part.get("state"), dict) else {}
            status = state.get("status")
            if status == "error":
                err = state.get("error")
                # Resource/tool permission denial patterns from spike
                if isinstance(err, str) and "permission denied" in err.lower():
                    self.permission_denied = True
                    self.denied_actions.append(
                        {
                            "tool": ev.part.get("tool"),
                            "input": state.get("input"),
                            "error": err,
                        }
                    )
            return
        if ev.type == EventType.ERROR and ev.error:
            self.top_level_error = ev.error
            return
        if ev.type == EventType.STEP_FINISH:
            return

    def parse_stream(self, stdout: str) -> list[NdjsonEvent]:
        for line in stdout.splitlines():
            self.parse_line(line)
        return self.events
