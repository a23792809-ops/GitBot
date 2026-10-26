"""Database and migration tests."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from gitbot.db import Database, split_sql_statements
from gitbot.errors import DatabaseError, MigrationError


def _table_names(database: Database) -> set[str]:
    with database.connect() as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrate_creates_expected_tables(database: Database) -> None:
    tables = _table_names(database)
    assert {"schema_migrations", "repos", "settings", "audit_log"} <= tables


def test_migrate_records_checksum(database: Database) -> None:
    applied = database.applied_migrations()
    assert [migration.version for migration in applied] == ["0001_init"]
    assert len(applied[0].checksum) == 64
    assert applied[0].applied_at.endswith("Z")


def test_migrate_is_idempotent(database: Database) -> None:
    assert database.migrate() == []
    assert len(database.applied_migrations()) == 1


def test_editing_an_applied_migration_is_rejected(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schema"
    shutil.copytree(Path(__file__).parents[1] / "backend" / "gitbot" / "schema", schema_dir)
    database = Database(tmp_path / "db.sqlite", schema_dir=schema_dir)
    database.migrate()

    migration = schema_dir / "0001_init.sql"
    migration.write_text(migration.read_text(encoding="utf-8") + "\n-- tweak\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="modified after being applied"):
        database.migrate()


def test_bad_migration_filename_is_rejected(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    database = Database(tmp_path / "db.sqlite", schema_dir=schema_dir)

    with pytest.raises(MigrationError, match=r"0001_name\.sql"):
        database.migrate()


def test_missing_schema_dir_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite", schema_dir=tmp_path / "nope")
    with pytest.raises(MigrationError, match="schema directory is missing"):
        database.migrate()


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "0001_broken.sql").write_text(
        "CREATE TABLE good (id INTEGER);\nCREATE TABLE bad (id INTEGER REFERENCES missing(id));"
        "\nINSERT INTO nope VALUES (1);\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "db.sqlite", schema_dir=schema_dir)

    with pytest.raises(MigrationError, match="failed to apply migration 0001_broken"):
        database.migrate()

    assert "good" not in _table_names(database)
    assert database.applied_migrations() == []


def test_wal_mode_enabled_for_file_databases(database: Database) -> None:
    with database.connect() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_enabled(database: Database) -> None:
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_unopenable_database_raises_database_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")

    database = Database(blocker / "nested" / "db.sqlite")
    with pytest.raises(DatabaseError, match="cannot open database"), database.connect():
        pass


def test_audit_log_is_append_only_at_the_sql_level(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO audit_log (created_at, event_type, actor, action, outcome) "
            "VALUES ('2026-01-01T00:00:00Z', 'test', 'system', 'seed', 'info')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_log SET action = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_log")


def test_repos_table_rejects_duplicate_paths(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO repos (path, slug, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("/tmp/example", "owner/example", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO repos (path, slug, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("/tmp/example", "owner/other", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )


def test_split_sql_statements_keeps_trigger_bodies_intact() -> None:
    script = """
    CREATE TABLE t (id INTEGER);
    CREATE TRIGGER t_no_delete
    BEFORE DELETE ON t
    BEGIN
        SELECT RAISE(ABORT, 'no; deleting');
    END;
    """
    statements = split_sql_statements(script)
    assert len(statements) == 2
    assert "no; deleting" in statements[1]


def test_split_sql_statements_rejects_unterminated_script() -> None:
    with pytest.raises(MigrationError, match="incomplete statement"):
        split_sql_statements("CREATE TABLE t (id INTEGER)")
