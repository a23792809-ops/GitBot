"""Security helpers: redaction, local token, and repository allowlisting."""

from __future__ import annotations

from gitbot.security.allowlist import AllowlistError, resolve_repo_path
from gitbot.security.redact import (
    RedactionFilter,
    contains_secret,
    redact_text,
    redact_value,
)
from gitbot.security.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxCommand,
    SandboxRunResult,
    SandboxUnavailable,
    SnapshotError,
    build_sandbox_command,
    create_repository_snapshot,
    detect_sandbox_capabilities,
    isolated_repository,
    require_sandbox,
    run_sandbox_command,
    validate_source_repository,
)
from gitbot.security.tokens import (
    ensure_token,
    read_token,
    rotate_token,
    tokens_match,
)

__all__ = [
    "AllowlistError",
    "RedactionFilter",
    "Sandbox",
    "SandboxCapabilities",
    "SandboxCommand",
    "SandboxRunResult",
    "SandboxUnavailable",
    "SnapshotError",
    "build_sandbox_command",
    "contains_secret",
    "create_repository_snapshot",
    "detect_sandbox_capabilities",
    "ensure_token",
    "isolated_repository",
    "read_token",
    "redact_text",
    "redact_value",
    "require_sandbox",
    "resolve_repo_path",
    "rotate_token",
    "run_sandbox_command",
    "tokens_match",
    "validate_source_repository",
]
