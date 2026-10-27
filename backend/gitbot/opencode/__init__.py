"""OpenCode integration (subprocess adapter)."""

from __future__ import annotations

from gitbot.opencode.adapter import (
    OpenCodeAdapter,
    OpenCodeConfig,
    OpenCodeResult,
    RunClassification,
)
from gitbot.opencode.parser import (
    EventType,
    NdjsonEvent,
    OpenCodeEventParser,
    ToolState,
)
from gitbot.opencode.policy import PermissionRule, build_project_opencode_config

__all__ = [
    "EventType",
    "NdjsonEvent",
    "OpenCodeAdapter",
    "OpenCodeConfig",
    "OpenCodeEventParser",
    "OpenCodeResult",
    "PermissionRule",
    "RunClassification",
    "ToolState",
    "build_project_opencode_config",
]
