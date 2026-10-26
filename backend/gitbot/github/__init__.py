"""GitHub integration adapters (read-only in Phase 1)."""

from __future__ import annotations

from gitbot.github.client import GhClient, parse_oauth_scopes

__all__ = ["GhClient", "parse_oauth_scopes"]
