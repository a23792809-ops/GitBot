"""Diff-hash-bound, single-use, expiring approvals."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from gitbot.db import Database
from gitbot.errors import ApprovalExpired, ApprovalInvalid, ApprovalMismatch
from gitbot.models import ApprovalAction


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    task_id: str
    repo_id: int
    action: ApprovalAction
    diff_hash: str
    argv: tuple[str, ...]
    expires_at: str
    used_at: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_diff_hash(patch: str) -> str:
    h = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    return h


def generate_approval_id() -> str:
    return f"apr_{secrets.token_urlsafe(16)}"


class ApprovalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        task_id: str,
        repo_id: int,
        action: ApprovalAction,
        diff_hash: str,
        argv: list[str],
        expires_minutes: int = 30,
        actor: str = "system",
    ) -> Approval:
        approval_id = generate_approval_id()
        now = _now()
        expires_at = (datetime.now(UTC) + timedelta(minutes=expires_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self.db.connect() as conn:
            # Ensure task exists for FK
            cur = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
            if cur.fetchone() is None:
                conn.execute(
                    "INSERT INTO tasks ("
                    "id, repo_id, task_type, state, prompt, created_at, updated_at"
                    ") VALUES (?, ?, 'analyze', 'queued', '', ?, ?)",
                    (task_id, repo_id, now, now),
                )
            conn.execute(
                "INSERT INTO approvals ("
                "id, task_id, repo_id, action, diff_hash, argv_json, expires_at, created_at, actor"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    task_id,
                    repo_id,
                    action.value,
                    diff_hash,
                    json.dumps(argv),
                    expires_at,
                    now,
                    actor,
                ),
            )
        return Approval(
            id=approval_id,
            task_id=task_id,
            repo_id=repo_id,
            action=action,
            diff_hash=diff_hash,
            argv=tuple(argv),
            expires_at=expires_at,
            used_at=None,
            created_at=now,
        )

    def get(self, approval_id: str) -> Approval:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalInvalid("approval not found")
        argv = tuple(json.loads(row["argv_json"]))
        return Approval(
            id=row["id"],
            task_id=row["task_id"],
            repo_id=row["repo_id"],
            action=ApprovalAction(row["action"]),
            diff_hash=row["diff_hash"],
            argv=argv,
            expires_at=row["expires_at"],
            used_at=row["used_at"],
            created_at=row["created_at"],
        )

    def consume(self, approval_id: str, *, task_id: str, repo_id: int, diff_hash: str) -> Approval:
        approval = self.get(approval_id)
        if approval.used_at is not None:
            raise ApprovalInvalid("approval already used")
        # Check expiry
        now = datetime.now(UTC)
        try:
            exp = datetime.strptime(approval.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except Exception as exc:
            raise ApprovalInvalid("invalid approval expiry") from exc
        if now >= exp:
            raise ApprovalExpired("approval expired")
        if approval.task_id != task_id or approval.repo_id != repo_id:
            raise ApprovalMismatch("approval does not match task/repo")
        if approval.diff_hash != diff_hash:
            raise ApprovalMismatch("diff hash mismatch")
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE approvals SET used_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE id = ? AND used_at IS NULL",
                (approval_id,),
            )
        return approval
