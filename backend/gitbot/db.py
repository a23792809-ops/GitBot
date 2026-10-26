"""SQLite access and forward-only, checksum-verified migrations.

Design notes:
- No ORM. Hand-written SQL keeps Python 3.14 compatibility surface small.
- WAL mode with a single writer; every connection sets ``foreign_keys`` and a
  busy timeout so concurrent readers do not fail spuriously.
- Applied migrations are immutable: editing one raises ``MigrationError``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from gitbot.errors import DatabaseError, MigrationError

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
MIGRATION_TABLE = "schema_migrations"
_MIGRATION_FILENAME = re.compile(r"^(?P<version>\d{4}_[a-z0-9_]+)\.sql$")

# Table names are module constants, never user input; kept out of f-strings in
# execute() calls so static analysis can verify the rest of the SQL is literal.
_SELECT_APPLIED = f"SELECT version, checksum, applied_at FROM {MIGRATION_TABLE} ORDER BY version"  # noqa: S608
_SELECT_CHECKSUMS = f"SELECT version, checksum FROM {MIGRATION_TABLE}"  # noqa: S608
_INSERT_MIGRATION = (
    f"INSERT INTO {MIGRATION_TABLE} (version, checksum, applied_at) "  # noqa: S608
    "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
)


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: str
    checksum: str
    applied_at: str


def split_sql_statements(script: str) -> list[str]:
    """Split a script into complete statements without tearing string literals.

    ``sqlite3.complete_statement`` understands quoting and triggers, which is why
    it is used instead of splitting on semicolons.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement and statement.strip(";").strip():
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration script ends with an incomplete statement")
    return statements


class Database:
    """A thin owner of the SQLite file and its schema version."""

    def __init__(self, path: Path | str, *, schema_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.schema_dir = Path(schema_dir) if schema_dir is not None else DEFAULT_SCHEMA_DIR
        self._is_memory = self.path == Path(":memory:")

    # -- connections -----------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            if not self._is_memory:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(
                f"cannot open database at {self.path}", details={"path": str(self.path)}
            ) from exc

        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if not self._is_memory:
                connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        finally:
            connection.close()

    # -- migrations ------------------------------------------------------
    def applied_migrations(self) -> list[AppliedMigration]:
        with self.connect() as connection:
            if not self._table_exists(connection, MIGRATION_TABLE):
                return []
            rows = connection.execute(_SELECT_APPLIED).fetchall()
            return [
                AppliedMigration(
                    version=row["version"], checksum=row["checksum"], applied_at=row["applied_at"]
                )
                for row in rows
            ]

    def migrate(self) -> list[str]:
        """Apply pending migrations in order. Returns newly applied versions."""
        files = self._migration_files()
        with self.connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
                    version    TEXT PRIMARY KEY,
                    checksum   TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]: row["checksum"]
                for row in connection.execute(_SELECT_CHECKSUMS).fetchall()
            }

            newly_applied: list[str] = []
            for version, path in files:
                script = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()

                if version in applied:
                    if applied[version] != checksum:
                        raise MigrationError(
                            f"migration {version} was modified after being applied",
                            details={"version": version, "path": str(path)},
                        )
                    continue

                self._apply(connection, version, script, checksum)
                newly_applied.append(version)

            return newly_applied

    def _apply(
        self, connection: sqlite3.Connection, version: str, script: str, checksum: str
    ) -> None:
        statements = split_sql_statements(script)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(_INSERT_MIGRATION, (version, checksum))
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise MigrationError(
                f"failed to apply migration {version}: {exc}",
                details={"version": version},
            ) from exc

    def _migration_files(self) -> list[tuple[str, Path]]:
        if not self.schema_dir.is_dir():
            raise MigrationError(
                "schema directory is missing", details={"path": str(self.schema_dir)}
            )
        found: list[tuple[str, Path]] = []
        for path in sorted(self.schema_dir.glob("*.sql")):
            match = _MIGRATION_FILENAME.match(path.name)
            if not match:
                raise MigrationError(
                    f"migration filename must look like 0001_name.sql, got {path.name}",
                    details={"path": str(path)},
                )
            found.append((match.group("version"), path))
        if not found:
            raise MigrationError("no migration files found", details={"path": str(self.schema_dir)})
        return found

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None
