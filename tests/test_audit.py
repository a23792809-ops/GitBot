"""Audit log tests: ordering, paging, and redaction at the write boundary."""

from __future__ import annotations

import json

import pytest

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.models import AuditOutcome

GH_TOKEN = "gh" + "p_" + ("Z9y8X7w6" * 5)


def test_record_and_read_back(audit_log: AuditLog) -> None:
    entry_id = audit_log.record(
        event_type="app.start",
        action="start",
        actor="system",
        target="localhost",
        outcome=AuditOutcome.SUCCESS,
        detail={"host": "127.0.0.1", "port": 8765},
    )

    entries = audit_log.list_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == entry_id
    assert entry.event_type == "app.start"
    assert entry.outcome == "success"
    assert entry.detail == {"host": "127.0.0.1", "port": 8765}
    assert entry.created_at.endswith("Z")


def test_entries_are_newest_first_and_pageable(audit_log: AuditLog) -> None:
    ids = [audit_log.record(event_type="task.event", action=f"step-{i}") for i in range(5)]

    newest_first = audit_log.list_entries(limit=10)
    assert [entry.id for entry in newest_first] == list(reversed(ids))

    page = audit_log.list_entries(limit=2, before_id=ids[3])
    assert [entry.id for entry in page] == [ids[2], ids[1]]


def test_event_type_filter(audit_log: AuditLog) -> None:
    audit_log.record(event_type="github.read", action="list_repos")
    audit_log.record(event_type="task.event", action="queued")

    github_entries = audit_log.list_entries(event_type="github.read")
    assert len(github_entries) == 1
    assert github_entries[0].action == "list_repos"


def test_limit_is_clamped(audit_log: AuditLog) -> None:
    audit_log.record(event_type="task.event", action="one")
    assert len(audit_log.list_entries(limit=0)) == 1
    assert len(audit_log.list_entries(limit=10_000)) == 1


def test_secrets_in_detail_are_redacted_before_storage(
    database: Database, audit_log: AuditLog
) -> None:
    audit_log.record(
        event_type="github.read",
        action="auth_check",
        detail={"note": f"token was {GH_TOKEN}", "nested": [GH_TOKEN]},
    )

    with database.connect() as connection:
        raw = connection.execute("SELECT detail FROM audit_log").fetchone()["detail"]

    assert GH_TOKEN not in raw
    stored = json.loads(raw)
    assert "[REDACTED:github_token]" in stored["note"]
    assert all("[REDACTED" in value for value in stored["nested"])


def test_detail_roundtrips_non_string_values(audit_log: AuditLog) -> None:
    audit_log.record(
        event_type="task.event",
        action="retry",
        detail={"attempt": 2, "ok": False, "tags": ["a", "b"], "nothing": None},
    )
    entry = audit_log.list_entries()[0]
    assert entry.detail == {"attempt": 2, "ok": False, "tags": ["a", "b"], "nothing": None}


def test_unknown_outcome_is_rejected_by_constraint(audit_log: AuditLog) -> None:
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        audit_log.record(event_type="task.event", action="weird", outcome="banana")
