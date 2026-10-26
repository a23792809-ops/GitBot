"""Typed error hierarchy.

Every failure that can reach the API or the audit log is expressed here so that
callers never have to inspect raw subprocess output or emit stack traces.
"""

from __future__ import annotations

from typing import Any


class GitBotError(Exception):
    """Base class for all expected GitBot failures."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ConfigError(GitBotError):
    code = "config_error"
    http_status = 500


class PolicyViolation(GitBotError):
    """An action was refused by an explicit project safety rule."""

    code = "policy_violation"
    http_status = 403


class NotFoundError(GitBotError):
    code = "not_found"
    http_status = 404


class DatabaseError(GitBotError):
    code = "database_error"
    http_status = 500


class MigrationError(DatabaseError):
    code = "migration_error"


class UpstreamError(GitBotError):
    """A local CLI (gh, git, opencode) failed in a way we could not interpret."""

    code = "upstream_error"
    http_status = 502


class GhNotAuthenticated(UpstreamError):
    code = "gh_not_authenticated"
    http_status = 401


class UpstreamTimeout(UpstreamError):
    code = "upstream_timeout"
    http_status = 504
