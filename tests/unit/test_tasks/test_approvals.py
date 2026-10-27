"""Tests for approvals (diff-hash bound, single-use, expiring)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gitbot.db import Database
from gitbot.errors import ApprovalExpired, ApprovalInvalid, ApprovalMismatch
from gitbot.models import ApprovalAction
from gitbot.tasks.approvals import ApprovalStore, compute_diff_hash


def _create_repo(db: Database) -> int:
    from gitbot.timeutil import utc_now_iso

    now = utc_now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO repos ("
            "path, slug, default_branch, allow_commit, allow_push, created_at, updated_at"
            ") VALUES (?, ?, ?, 0, 0, ?, ?)",
            ("/tmp/testrepo", "local/testrepo", "main", now, now),
        )
        return int(cur.lastrowid)


def test_approval_create_consume(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store = ApprovalStore(db)
    diff_hash = compute_diff_hash("diff --git a/x b/x\n")
    approval = store.create("t1", repo_id, ApprovalAction.COMMIT, diff_hash, ["git", "commit"])
    consumed = store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash=diff_hash)
    assert consumed.id == approval.id
    with pytest.raises(ApprovalInvalid):
        store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash=diff_hash)


def test_approval_mismatch(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store = ApprovalStore(db)
    diff_hash = compute_diff_hash("diff")
    approval = store.create("t1", repo_id, ApprovalAction.COMMIT, diff_hash, ["git", "commit"])
    with pytest.raises(ApprovalMismatch):
        store.consume(approval.id, task_id="t2", repo_id=repo_id, diff_hash=diff_hash)
    with pytest.raises(ApprovalMismatch):
        store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash="wronghash")


def test_approval_expired(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store = ApprovalStore(db)
    diff_hash = compute_diff_hash("diff")
    approval = store.create(
        "t1", repo_id, ApprovalAction.COMMIT, diff_hash, ["git", "commit"], expires_minutes=0
    )
    time.sleep(1)
    with pytest.raises((ApprovalExpired, ApprovalInvalid)):
        store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash=diff_hash)
