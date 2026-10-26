"""Small time helpers so every timestamp in GitBot has one format."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """UTC ISO-8601 with a ``Z`` suffix, matching SQLite's strftime output."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
