"""Task queue helpers."""

from __future__ import annotations

from gitbot.db import Database


class TaskQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    def claim_next(self) -> dict | None:
        # Simple claim - in single process context
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET state = 'running', started_at = COALESCE(started_at, "
                "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND state = 'queued'",
                (row["id"],),
            )
            row2 = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            return dict(row2) if row2 else None
