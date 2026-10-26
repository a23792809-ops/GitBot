"""Allowlisted repositories. Phase 1 exposes reads only; writes arrive in Phase 2."""

from __future__ import annotations

from gitbot.db import Database
from gitbot.models import Repo


class RepoStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list(self) -> list[Repo]:
        with self._db.connect() as connection:
            rows = connection.execute("SELECT * FROM repos ORDER BY slug COLLATE NOCASE").fetchall()
        return [Repo.from_row(row) for row in rows]

    def count(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM repos").fetchone()
        return int(row["n"])
