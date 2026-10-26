"""Shared fixtures. All tests are hermetic: no real gh, git, or network calls."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env={"HOME": str(tmp_path)})


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "gitbot.db"


@pytest.fixture
def database(db_path: Path) -> Iterator[Database]:
    database = Database(db_path)
    database.migrate()
    yield database


@pytest.fixture
def audit_log(database: Database) -> AuditLog:
    return AuditLog(database)


@pytest.fixture
def fake_gh(tmp_path: Path) -> Iterator[Path]:
    """An executable stand-in for the real `gh`; no network, no credentials."""
    source = Path(__file__).parent / "fixtures" / "fake_gh.py"
    script = tmp_path / "fake-gh"
    script.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)
    yield script
