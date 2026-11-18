"""Allowlisted repositories. Reads for everyone; writes require API auth at the route."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from gitbot.db import Database
from gitbot.errors import ConflictError, PolicyViolation
from gitbot.models import ALL_TASK_TYPES, Repo, TaskType
from gitbot.timeutil import utc_now_iso

_DEFAULT_TASK_TYPES = ",".join(task_type.value for task_type in ALL_TASK_TYPES)
_MAX_TEST_COMMAND_CHARS = 500


def normalize_task_types(values: Sequence[str | TaskType] | None) -> str:
    """Return a canonical, comma-separated allowlist of task types."""
    if values is None:
        return _DEFAULT_TASK_TYPES
    if isinstance(values, str):  # defensive: a CSV string is not a sequence of types
        raise PolicyViolation("allowed_task_types must be a list of task types")
    seen: list[str] = []
    for value in values:
        try:
            task_type = value if isinstance(value, TaskType) else TaskType(str(value))
        except ValueError as exc:
            raise PolicyViolation(
                f"unknown task type: {value}",
                details={"allowed": [item.value for item in ALL_TASK_TYPES]},
            ) from exc
        if task_type.value not in seen:
            seen.append(task_type.value)
    if not seen:
        raise PolicyViolation("at least one task type must be allowed")
    canonical = [task_type.value for task_type in ALL_TASK_TYPES if task_type.value in seen]
    return ",".join(canonical)


def normalize_test_command(value: str | None) -> str | None:
    """Validate the user-supplied, host-configured test command.

    The command is split with :func:`shlex.split` and executed as argv inside the
    disposable sandbox; it is never passed to a shell. Repository content can
    never set this value.
    """
    if value is None:
        return None
    command = value.strip()
    if not command:
        return None
    if len(command) > _MAX_TEST_COMMAND_CHARS:
        raise PolicyViolation(f"test_command must be at most {_MAX_TEST_COMMAND_CHARS} characters")
    if any(ord(char) < 32 for char in command):
        raise PolicyViolation("test_command must be a single line without control characters")
    return command


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
        allowed_task_types: Sequence[str | TaskType] | None = None,
        test_command: str | None = None,
    ) -> Repo:
        now = utc_now_iso()
        allowed = normalize_task_types(allowed_task_types)
        command = normalize_test_command(test_command)
        with self._db.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO repos
                        (path, slug, default_branch, allow_commit, allow_push,
                         allowed_task_types, test_command, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        path,
                        slug,
                        default_branch,
                        int(allow_commit),
                        int(allow_push),
                        allowed,
                        command,
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
        allowed_task_types: Sequence[str | TaskType] | None = None,
        test_command: str | None = None,
        clear_test_command: bool = False,
    ) -> Repo | None:
        fields: dict[str, Any] = {}
        if allow_commit is not None:
            fields["allow_commit"] = int(allow_commit)
        if allow_push is not None:
            fields["allow_push"] = int(allow_push)
        if allowed_task_types is not None:
            fields["allowed_task_types"] = normalize_task_types(allowed_task_types)
        if test_command is not None:
            fields["test_command"] = normalize_test_command(test_command)
        elif clear_test_command:
            fields["test_command"] = None
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
        try:
            with self._db.connect() as connection:
                cursor = connection.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
                return cursor.rowcount > 0
        except sqlite3.IntegrityError as exc:
            # tasks/approvals/task_events reference repos with ON DELETE RESTRICT.
            # Keeping the history is intentional: removing the allowlist entry
            # must never erase audit records.
            raise ConflictError(
                "repository has task history and cannot be removed; "
                "it can be made read-only instead",
                details={"repo_id": repo_id},
            ) from exc
