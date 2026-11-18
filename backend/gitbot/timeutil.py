"""Small time helpers so every timestamp in GitBot has one format."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """UTC ISO-8601 with a ``Z`` suffix, matching SQLite's strftime output."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_utc(value: datetime) -> str:
    """Format an aware datetime in GitBot's canonical UTC form."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and require an explicit timezone.

    A naive timestamp is rejected rather than silently assumed to be local time.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)
