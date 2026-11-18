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
    assert {"schema_migrations", "repos", "settings", "audit_log", "repo_schedules"} <= tables


def test_migrate_records_checksum(database: Database) -> None:
    applied = database.applied_migrations()
    assert [migration.version for migration in applied] == [
        "0001_init",
        "0002_tasks",
        "0003_task_output",
        "0004_task_review",
        "0005_analysis_state",
        "0006_explain_scheduler_tests",
        "0007_daily_schedules",
    ]
    assert len(applied[0].checksum) == 64
    assert applied[0].applied_at.endswith("Z")


def test_migrate_is_idempotent(database: Database) -> None:
    assert database.migrate() == []
    assert len(database.applied_migrations()) == 7


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


def _schema_dir_through(tmp_path: Path, versions: tuple[str, ...]) -> Path:
    source = Path(__file__).parents[1] / "backend" / "gitbot" / "schema"
    target = tmp_path / "schema-partial"
    target.mkdir()
    for version in versions:
        shutil.copy2(source / f"{version}.sql", target / f"{version}.sql")
    return target


def test_migration_0005_upgrades_a_populated_0004_database(tmp_path: Path) -> None:
    partial = _schema_dir_through(
        tmp_path,
        ("0001_init", "0002_tasks", "0003_task_output", "0004_task_review"),
    )
    db_path = tmp_path / "gitbot.db"
    database = Database(db_path, schema_dir=partial)
    assert database.migrate()[-1] == "0004_task_review"

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO repos (id, path, slug, created_at, updated_at) "
            "VALUES (1, '/tmp/old', 'old/repo', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO tasks (id, repo_id, task_type, state, prompt, created_at, updated_at, "
            "output, diff_hash) VALUES ('tsk_old', 1, 'analyze', 'awaiting_review', 'old prompt', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'old answer', 'abc')"
        )
        connection.execute(
            "INSERT INTO approvals (id, task_id, repo_id, action, diff_hash, expires_at, "
            "created_at, actor) VALUES ('apr_old', 'tsk_old', 1, 'commit', 'abc', "
            "'2026-01-02T00:00:00Z', '2026-01-01T00:00:00Z', 'system')"
        )
        connection.execute(
            "INSERT INTO task_events (task_id, created_at, event_type, actor) "
            "VALUES ('tsk_old', '2026-01-01T00:00:00Z', 'task.created', 'system')"
        )

    source = Path(__file__).parents[1] / "backend" / "gitbot" / "schema"
    upgraded = Database(db_path, schema_dir=source)
    assert upgraded.migrate() == [
        "0005_analysis_state",
        "0006_explain_scheduler_tests",
        "0007_daily_schedules",
    ]

    with upgraded.connect() as connection:
        task = connection.execute("SELECT * FROM tasks WHERE id = 'tsk_old'").fetchone()
        assert task["state"] == "awaiting_review"
        assert task["output"] == "old answer"
        assert task["diff_hash"] == "abc"
        assert task["sandboxed"] == 0
        assert task["snapshot_head"] is None
        assert task["scheduled_at"] is None
        assert task["retry_count"] == 0
        assert task["max_retries"] == 0
        assert task["parent_task_id"] is None
        assert task["tests_status"] == "not_run"
        assert task["tests_output"] is None
        assert task["tests_command"] is None
        assert task["test_exit_code"] is None
        repo = connection.execute("SELECT * FROM repos WHERE id = 1").fetchone()
        assert repo["path"] == "/tmp/old"
        assert repo["allowed_task_types"] == "analyze,explain,bugfix,feature,docs,deps,tests"
        assert repo["test_command"] is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1

        # The rebuilt table accepts the new task types and terminal states.
        connection.execute("UPDATE tasks SET state = 'analyzed' WHERE id = 'tsk_old'")
        connection.execute(
            "INSERT INTO tasks (id, repo_id, task_type, state, prompt, created_at, updated_at) "
            "VALUES ('tsk_explain', 1, 'explain', 'explained', 'explain it', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tasks SET state = 'not-a-state' WHERE id = 'tsk_old'")

        # Foreign keys are still enforced after a table rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor) "
                "VALUES ('missing-task', '2026-01-01T00:00:00Z', 'x', 'system')"
            )


def test_applied_migration_without_a_file_is_rejected(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "0001_first.sql").write_text("CREATE TABLE first (id INTEGER);", encoding="utf-8")
    (schema_dir / "0002_second.sql").write_text(
        "CREATE TABLE second (id INTEGER);", encoding="utf-8"
    )
    database = Database(tmp_path / "db.sqlite", schema_dir=schema_dir)
    database.migrate()

    (schema_dir / "0001_first.sql").unlink()
    with pytest.raises(MigrationError, match="has no schema file"):
        database.migrate()


def test_pending_migration_cannot_be_applied_out_of_order(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "0002_second.sql").write_text(
        "CREATE TABLE second (id INTEGER);", encoding="utf-8"
    )
    database = Database(tmp_path / "db.sqlite", schema_dir=schema_dir)
    assert database.migrate() == ["0002_second"]

    (schema_dir / "0001_first.sql").write_text("CREATE TABLE first (id INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError, match="out of order"):
        database.migrate()


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
