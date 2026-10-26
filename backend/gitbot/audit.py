"""Append-only audit trail with redaction applied before every write."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gitbot.db import Database
from gitbot.models import AuditOutcome
from gitbot.security import redact_value

MAX_LIMIT = 500


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    created_at: str
    event_type: str
    actor: str
    action: str
    target: str | None
    outcome: str
    detail: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> AuditEntry:
        try:
            detail = json.loads(row["detail"] or "{}")
        except json.JSONDecodeError:
            detail = {"unparsed_detail": True}
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            event_type=row["event_type"],
            actor=row["actor"],
            action=row["action"],
            target=row["target"],
            outcome=row["outcome"],
            detail=detail if isinstance(detail, dict) else {"detail": detail},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "event_type": self.event_type,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def _now() -> str:
    """UTC timestamp in the same ``...Z`` form SQLite writes for migrations."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class AuditLog:
    """Write and read audit rows. There is deliberately no update or delete."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def record(
        self,
        *,
        event_type: str,
        action: str,
        actor: str = "system",
        target: str | None = None,
        outcome: AuditOutcome | str = AuditOutcome.INFO,
        detail: dict[str, Any] | None = None,
    ) -> int:
        outcome_value = str(outcome)
        payload = json.dumps(redact_value(detail or {}), sort_keys=True)
        with self._db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_log
                    (created_at, event_type, actor, action, target, outcome, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now(), event_type, actor, action, target, outcome_value, payload),
            )
            return int(cursor.lastrowid or 0)

    def list_entries(
        self,
        *,
        limit: int = 50,
        before_id: int | None = None,
        event_type: str | None = None,
    ) -> list[AuditEntry]:
        bounded = max(1, min(limit, MAX_LIMIT))
        clauses: list[str] = []
        params: list[Any] = []
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(bounded)
        with self._db.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
        return [AuditEntry.from_row(row) for row in rows]
