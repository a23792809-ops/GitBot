"""Allowlisted repositories. Reads for everyone; writes require API auth at the route."""

from __future__ import annotations

import sqlite3
from typing import Any

from gitbot.db import Database
from gitbot.errors import ConflictError
from gitbot.models import Repo
from gitbot.timeutil import utc_now_iso


class RepoStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list(self) -> list[Repo]:
        with self._db.connect() as connection:
            rows = connection.execute("SELECT * FROM repos ORDER BY slug COLLATE NOCASE").fetchall()
        return [Repo.from_row(row) for row in rows]

    def get(self, repo_id: int) -> Repo | None:
        with self._db.connect() as connection:
            row = connection.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return Repo.from_row(row) if row is not None else None

    def find_by_path(self, path: str) -> Repo | None:
        with self._db.connect() as connection:
            row = connection.execute("SELECT * FROM repos WHERE path = ?", (path,)).fetchone()
        return Repo.from_row(row) if row is not None else None

    def count(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM repos").fetchone()
        return int(row["n"])

    def add(
        self,
        *,
        path: str,
        slug: str,
        default_branch: str = "main",
        allow_commit: bool = False,
        allow_push: bool = False,
    ) -> Repo:
        now = utc_now_iso()
        with self._db.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO repos
                        (path, slug, default_branch, allow_commit, allow_push,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        path,
                        slug,
                        default_branch,
                        int(allow_commit),
                        int(allow_push),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "repository is already allowlisted",
                    details={"path": path},
                ) from exc
            row_id = int(cursor.lastrowid or 0)
        created = self.get(row_id)
        if created is None:  # pragma: no cover - insert just happened
            raise ConflictError("repository insert could not be read back")
        return created

    def update(
        self,
        repo_id: int,
        *,
        allow_commit: bool | None = None,
        allow_push: bool | None = None,
    ) -> Repo | None:
        fields: dict[str, Any] = {}
        if allow_commit is not None:
            fields["allow_commit"] = int(allow_commit)
        if allow_push is not None:
            fields["allow_push"] = int(allow_push)
        if not fields:
            return self.get(repo_id)
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._db.connect() as connection:
            connection.execute(
                f"UPDATE repos SET {assignments} WHERE id = ?",  # noqa: S608 - column names internal
                (*fields.values(), repo_id),
            )
        return self.get(repo_id)

    def delete(self, repo_id: int) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
            return cursor.rowcount > 0
