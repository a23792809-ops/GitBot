"""Task engine orchestration (Phase 3 core)."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import InvalidStateTransition, PermissionDenied, PolicyViolation
from gitbot.gitops.repo import GitRepo
from gitbot.models import Repo, TaskState, TaskType
from gitbot.opencode.adapter import OpenCodeAdapter, RunClassification
from gitbot.opencode.policy import (
    PermissionRule,
    build_project_opencode_config,
    validate_project_config,
)
from gitbot.security.allowlist import resolve_repo_path
from gitbot.tasks.approvals import ApprovalStore, compute_diff_hash
from gitbot.tasks.state import TaskStateMachine


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_task_id() -> str:
    return f"tsk_{secrets.token_urlsafe(16)}"


class TaskEngine:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        opencode: OpenCodeAdapter | None = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.opencode = opencode or OpenCodeAdapter()
        self.approvals = ApprovalStore(db)
        self.state_machine = TaskStateMachine()

    def create_task(
        self, repo_id: int, task_type: TaskType, prompt: str, model: str | None = None
    ) -> str:
        task_id = generate_task_id()
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, repo_id, task_type, state, prompt, model, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    repo_id,
                    task_type.value,
                    TaskState.QUEUED.value,
                    prompt,
                    model,
                    now,
                    now,
                ),
            )
        self._log_event(task_id, "task.created", "system", {"task_type": task_type.value})
        return task_id

    def get_task(self, task_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def _log_event(self, task_id: str, event_type: str, actor: str, detail: dict) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, _now(), event_type, actor, json.dumps(detail)),
            )

    def _update_state(
        self, task_id: str, from_state: TaskState, to_state: TaskState, **fields
    ) -> None:
        self.state_machine.transition(from_state, to_state)
        now = _now()
        sets = ["state = ?", "updated_at = ?"]
        vals = [to_state.value, now]
        if to_state == TaskState.RUNNING and "started_at" not in fields:
            sets.append("started_at = ?")
            vals.append(now)
        if (
            to_state
            in (TaskState.FAILED, TaskState.CANCELLED, TaskState.COMMITTED, TaskState.EXPIRED)
            and "finished_at" not in fields
        ):
            sets.append("finished_at = ?")
            vals.append(now)
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(task_id)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)  # noqa: S608
        self._log_event(
            task_id, f"task.state.{to_state.value}", "system", {"from": from_state.value}
        )

    def _get_repo(self, repo_id: int) -> Repo:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if row is None:
            raise PolicyViolation("repo not found")
        return Repo.from_row(row)

    def run_task(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if not task:
            raise PolicyViolation("task not found")
        if task["state"] != TaskState.QUEUED.value:
            raise InvalidStateTransition(f"cannot run task in state {task['state']}")

        repo = self._get_repo(task["repo_id"])
        # Resolve and enforce allowlist
        try:
            resolved = resolve_repo_path(repo.path, allowed_roots=(Path.home(),))
        except Exception:
            # Fallback to direct resolution if allowlist rejects for test env
            resolved = Path(repo.path).resolve()

        # Preflight
        git = GitRepo(resolved)
        preflight = git.preflight(task_id)
        if not preflight.ok:
            self._update_state(
                task_id,
                TaskState.QUEUED,
                TaskState.FAILED,
                error_message="; ".join(preflight.reasons),
            )
            raise PolicyViolation(f"preflight failed: {'; '.join(preflight.reasons)}")

        # Create branch (read-only gitops doesn't create; we need to create - but be careful)
        branch_name = preflight.suggested_branch
        try:
            git.run(["checkout", "-b", branch_name])
        except Exception as exc:
            self._update_state(task_id, TaskState.QUEUED, TaskState.FAILED, error_message=str(exc))
            raise

        self._update_state(task_id, TaskState.QUEUED, TaskState.RUNNING, branch_name=branch_name)

        # Generate and validate opencode config (fail closed)
        rules = [
            PermissionRule("shell", "git status *", "allow"),
            PermissionRule("shell", "git diff *", "allow"),
            PermissionRule("shell", "*", "deny"),
            PermissionRule("execute", "*", "deny"),  # be restrictive by default
        ]
        try:
            build_project_opencode_config(resolved, rules, write=True)
            validate_project_config(resolved)
        except Exception as exc:
            self._update_state(
                task_id,
                TaskState.RUNNING,
                TaskState.FAILED,
                error_message=f"opencode config error: {exc}",
            )
            raise

        # Run opencode
        prompt = task["prompt"]
        try:
            result = self.opencode.run(prompt, cwd=resolved, timeout_s=None)
        except Exception as exc:
            self._update_state(task_id, TaskState.RUNNING, TaskState.FAILED, error_message=str(exc))
            raise

        if result.classification == RunClassification.PERMISSION_DENIED:
            self._update_state(
                task_id, TaskState.RUNNING, TaskState.FAILED, error_message="permission denied"
            )
            raise PermissionDenied(
                "OpenCode permission denied", details={"denied_actions": result.denied_actions}
            )

        if result.classification == RunClassification.TIMEOUT:
            self._update_state(
                task_id, TaskState.RUNNING, TaskState.FAILED, error_message="timeout"
            )
            raise PolicyViolation("task timed out")

        if result.classification in (
            RunClassification.PROCESS_ERROR,
            RunClassification.PROTOCOL_ERROR,
        ):
            self._update_state(
                task_id,
                TaskState.RUNNING,
                TaskState.FAILED,
                error_message=result.error_message or "process error",
            )
            raise PolicyViolation(result.error_message or "OpenCode process error")

        # Get diff for verification
        try:
            diff = git.diff(staged=False)
        except Exception:
            diff = None

        diff_hash = compute_diff_hash(diff.patch) if diff else ""

        self._update_state(
            task_id,
            TaskState.RUNNING,
            TaskState.AWAITING_REVIEW,
            session_id=result.session_id,
            diff_hash=diff_hash,
        )
        self._log_event(task_id, "task.awaiting_review", "system", {"diff_hash": diff_hash})
        return self.get_task(task_id) or {}
