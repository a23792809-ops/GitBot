"""Per-task OpenCode permission policy generation and validation.

Follows Phase 3 spike guidance: generate a minimal project opencode.json,
validate schema, and fail closed if config is missing/ignored/unexpected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gitbot.errors import PolicyViolation


@dataclass(frozen=True, slots=True)
class PermissionRule:
    action: str
    resource: str
    effect: str


def _validate_rules(rules: list[PermissionRule]) -> None:
    allowed_actions = {"shell", "execute", "edit", "write", "read"}
    allowed_effects = {"allow", "deny", "ask"}
    for rule in rules:
        if rule.action not in allowed_actions:
            raise PolicyViolation(f"unsupported permission action: {rule.action}")
        if rule.effect not in allowed_effects:
            raise PolicyViolation(f"unsupported permission effect: {rule.effect}")
        if not rule.resource:
            raise PolicyViolation("permission resource cannot be empty")


def build_project_opencode_config(
    repo_path: Path | str,
    rules: list[PermissionRule],
    *,
    write: bool = True,
) -> Path:
    """Build a minimal V2-style permissions array and write opencode.json."""
    _validate_rules(rules)
    payload: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "permission": [
            {"action": rule.action, "resource": rule.resource, "effect": rule.effect}
            for rule in rules
        ],
    }
    target = Path(repo_path) / "opencode.json"
    if write:
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def validate_project_config(repo_path: Path | str) -> dict[str, Any]:
    """Validate that opencode.json exists and has expected structure."""
    target = Path(repo_path) / "opencode.json"
    if not target.exists():
        raise PolicyViolation("missing opencode.json in project directory")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PolicyViolation("opencode.json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PolicyViolation("opencode.json must be an object")
    perm = data.get("permission")
    if not isinstance(perm, list) or not perm:
        raise PolicyViolation("opencode.json must contain non-empty permissions array")
    # Basic structural validation
    for item in perm:
        if not isinstance(item, dict):
            raise PolicyViolation("permission entries must be objects")
        for key in ("action", "resource", "effect"):
            if key not in item:
                raise PolicyViolation(f"permission entry missing '{key}'")
    return data
