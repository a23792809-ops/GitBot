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


def _create_task(db: Database, task_id: str, repo_id: int) -> None:
    from gitbot.timeutil import utc_now_iso

    now = utc_now_iso()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, repo_id, task_type, state, prompt, created_at, updated_at) "
            "VALUES (?, ?, 'analyze', 'awaiting_review', 'review', ?, ?)",
            (task_id, repo_id, now, now),
        )


def _approval_for(db: Database, task_id: str, repo_id: int, diff: str, **kwargs):
    _create_task(db, task_id, repo_id)
    store = ApprovalStore(db)
    return store, store.create(
        task_id,
        repo_id,
        ApprovalAction.COMMIT,
        compute_diff_hash(diff),
        ["git", "commit"],
        **kwargs,
    )


def test_approval_create_consume(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store, approval = _approval_for(db, "t1", repo_id, "diff --git a/x b/x\n")
    diff_hash = compute_diff_hash("diff --git a/x b/x\n")
    consumed = store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash=diff_hash)
    assert consumed.id == approval.id
    with pytest.raises(ApprovalInvalid):
        store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash=diff_hash)


def test_approval_requires_existing_task(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store = ApprovalStore(db)
    with pytest.raises(ApprovalInvalid, match="task not found"):
        store.create("missing", repo_id, ApprovalAction.COMMIT, compute_diff_hash("diff"), [])


def test_approval_mismatch(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store, approval = _approval_for(db, "t1", repo_id, "diff")
    diff_hash = compute_diff_hash("diff")
    with pytest.raises(ApprovalMismatch):
        store.consume(approval.id, task_id="t2", repo_id=repo_id, diff_hash=diff_hash)
    with pytest.raises(ApprovalMismatch):
        store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash="wronghash")


def test_approval_expired(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    repo_id = _create_repo(db)
    store, approval = _approval_for(db, "t1", repo_id, "diff", expires_minutes=0)
    diff_hash = compute_diff_hash("diff")
    time.sleep(1)
    with pytest.raises((ApprovalExpired, ApprovalInvalid)):
        store.consume(approval.id, task_id="t1", repo_id=repo_id, diff_hash=diff_hash)
