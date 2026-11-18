"""Task engine orchestration (Phase 3 core)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gitbot.audit import AuditLog
from gitbot.db import Database
from gitbot.errors import (
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalMismatch,
    GitBotError,
    InvalidStateTransition,
    PermissionDenied,
    PolicyViolation,
)
from gitbot.gitops.repo import GitRepo, patch_touches_unsafe_git_metadata
from gitbot.models import (
    READ_ONLY_TASK_TYPES,
    ApprovalAction,
    Repo,
    TaskState,
    TaskType,
)
from gitbot.opencode.adapter import OpenCodeAdapter, RunClassification
from gitbot.opencode.analysis import run_analysis
from gitbot.opencode.changes import capture_staged_diff, run_change
from gitbot.opencode.gateway import GatewayConfig
from gitbot.security import redact_text, redact_value
from gitbot.security.allowlist import (
    PROJECT_ROOT,
    default_allowed_roots,
    resolve_repo_path,
)
from gitbot.tasks.approvals import ApprovalStore, compute_diff_hash
from gitbot.tasks.state import TaskStateMachine
from gitbot.timeutil import format_utc, parse_iso_utc

logger = logging.getLogger("gitbot.tasks.engine")

# One process owns task execution, so a module-level registry is the cancel
# channel between the request thread that runs a task and the request thread
# that asks to stop it. Entries are always removed in a ``finally``.
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()
# Poll the shared database while a run is active so another process (e.g. a
# second GitBot instance sharing this SQLite database) can also request
# cancellation by inserting a task.cancel_requested event.
_CANCEL_WATCH_INTERVAL_S = 0.5
_ACTIVE_RUNS: set[str] = set()
_ACTIVE_RUNS_LOCK = threading.Lock()
# Serialise commit against an approved-task cancellation in this process. The
# repository commit itself cannot participate in the SQLite transaction, so the
# lock prevents a commit from racing a cancel that consumes the same approval.
_COMMIT_LOCK = threading.Lock()


def _register_cancel(task_id: str) -> threading.Event:
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS[task_id] = event
    return event


def _release_cancel(task_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(task_id, None)


def request_cancel(task_id: str) -> bool:
    """Signal a running task to stop. Returns False when nothing is running."""
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(task_id)
    if event is None:
        return False
    event.set()
    return True


def _watch_cancel_requests(
    database: Database,
    task_id: str,
    cancel_event: threading.Event,
    not_before: str,
    stop_event: threading.Event,
) -> None:
    """Set ``cancel_event`` when another process asks to stop the task.

    Any process sharing the database can request cancellation by inserting a
    ``task.cancel_requested`` event; the owning process observes it here and
    raises the same in-memory signal used by same-process cancellation. Only
    requests created at or after ``not_before`` (the moment this run started)
    are honoured, so a stale request from an earlier run can never kill a new
    one. The thread exits when the run finishes or the database is
    unreadable.
    """
    while not stop_event.is_set():
        try:
            with database.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? "
                    "AND event_type = 'task.cancel_requested' AND created_at >= ? "
                    "LIMIT 1",
                    (task_id, not_before),
                ).fetchone()
        except Exception:  # a broken watchdog must not fail the run
            logger.exception("cancel-request watchdog failed for task %s", task_id)
            return
        if row is not None:
            cancel_event.set()
            return
        stop_event.wait(_CANCEL_WATCH_INTERVAL_S)


def _claim_run(task_id: str) -> bool:
    """Reserve a task for one request thread, preventing duplicate execution."""
    with _ACTIVE_RUNS_LOCK:
        if task_id in _ACTIVE_RUNS:
            return False
        _ACTIVE_RUNS.add(task_id)
        return True


def _release_run(task_id: str) -> None:
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.discard(task_id)


def recover_stale_tasks(database: Database) -> list[str]:
    """Fail tasks left ``running`` or mid-commit by a previous server process.

    A task can only be running in the process that owns its cancel event. When
    the server starts, no previous process exists, so keeping the row in
    ``running`` would make cancellation and inspection permanently ambiguous.
    The working tree and task branch are deliberately left untouched for the
    user to inspect; only the durable task state is recovered.

    A local commit is journaled with ``task.commit_started`` and terminated with
    ``task.committed``/``task.commit_failed``. An ``approved`` task that has a
    started-but-unterminated commit, or a consumed approval, is the residue of a
    crash between patch application, approval consumption, and the commit. It is
    failed and its approval is revoked so a stale approval can never be replayed.
    """
    recovered: list[str] = []
    now = _now()
    message = "server restarted while this task was running; inspect the working tree"
    interrupted_commit_message = (
        "server restarted during a local commit; inspect `git status` and `git log` "
        "before re-running this task"
    )
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE state = ?", (TaskState.RUNNING.value,)
        ).fetchall()
        for row in rows:
            task_id = str(row["id"])
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, finished_at = ?, error_message = ? "
                "WHERE id = ? AND state = ?",
                (
                    TaskState.FAILED.value,
                    now,
                    now,
                    message,
                    task_id,
                    TaskState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                continue
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, 'task.recovered.stale', 'system', ?)",
                (task_id, now, json.dumps({"previous_state": TaskState.RUNNING.value})),
            )
            recovered.append(task_id)

        interrupted = conn.execute(
            "SELECT t.id, t.approval_id FROM tasks t WHERE t.state = ? AND ("
            "  EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id "
            "          AND e.event_type = 'task.commit_started')"
            "  OR EXISTS (SELECT 1 FROM approvals a WHERE a.task_id = t.id "
            "             AND a.used_at IS NOT NULL)"
            ") AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id "
            "                  AND e.event_type = 'task.committed')",
            (TaskState.APPROVED.value,),
        ).fetchall()
        for row in interrupted:
            task_id = str(row["id"])
            approval_id = row["approval_id"]
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, finished_at = ?, error_message = ? "
                "WHERE id = ? AND state = ?",
                (
                    TaskState.FAILED.value,
                    now,
                    now,
                    interrupted_commit_message,
                    task_id,
                    TaskState.APPROVED.value,
                ),
            )
            if cursor.rowcount != 1:
                continue
            if approval_id:
                conn.execute(
                    "UPDATE approvals SET used_at = COALESCE("
                    "used_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) WHERE id = ?",
                    (approval_id,),
                )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, 'task.recovered.interrupted_commit', 'system', ?)",
                (
                    task_id,
                    now,
                    json.dumps(
                        {
                            "previous_state": TaskState.APPROVED.value,
                            "approval_id": approval_id,
                        }
                    ),
                ),
            )
            recovered.append(task_id)
    return recovered


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
        *,
        dry_run: bool = True,
        supervised_automation_enabled: bool = False,
        allow_approved_local_commits: bool = False,
        allowed_roots: Sequence[Path] | None = None,
        forbidden_roots: Sequence[Path] | None = None,
        verify_opencode_config: bool = True,
        sandbox_bwrap: str = "bwrap",
        gateway: GatewayConfig | None = None,
        retry_delay_s: int = 30,
    ) -> None:
        self.db = db
        self.audit = audit
        self.opencode = opencode or OpenCodeAdapter()
        self.approvals = ApprovalStore(db)
        self.state_machine = TaskStateMachine()
        self.dry_run = dry_run
        self.supervised_automation_enabled = bool(supervised_automation_enabled)
        self.allow_approved_local_commits = bool(allow_approved_local_commits)
        self.allowed_roots = (
            tuple(allowed_roots)
            if allowed_roots is not None
            else default_allowed_roots(Path.home())
        )
        self.forbidden_roots = (
            tuple(forbidden_roots) if forbidden_roots is not None else (PROJECT_ROOT,)
        )
        self.verify_opencode_config = verify_opencode_config
        self.sandbox_bwrap = sandbox_bwrap
        self.gateway = gateway
        self.retry_delay_s = max(0, int(retry_delay_s))

    @staticmethod
    def _normalize_scheduled_at(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return format_utc(parse_iso_utc(value))
        except (ValueError, TypeError) as exc:
            raise PolicyViolation(
                "scheduled_at must be an ISO-8601 timestamp with a timezone"
            ) from exc

    def create_task(
        self,
        repo_id: int,
        task_type: TaskType,
        prompt: str,
        model: str | None = None,
        *,
        scheduled_at: str | None = None,
        max_retries: int = 0,
        parent_task_id: str | None = None,
        retry_count: int = 0,
    ) -> str:
        repo = self._get_repo(repo_id)
        if not repo.allows_task_type(task_type):
            raise PolicyViolation(
                f"task type {task_type.value!r} is disabled for this repository",
                details={"repo_id": repo_id, "allowed": list(repo.allowed_task_types)},
            )
        if not 0 <= max_retries <= 5:
            raise PolicyViolation("max_retries must be between 0 and 5")
        if retry_count < 0 or retry_count > max_retries:
            raise PolicyViolation("retry_count is outside the configured retry budget")
        scheduled = self._normalize_scheduled_at(scheduled_at)
        task_id = generate_task_id()
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, repo_id, task_type, state, prompt, model, created_at, updated_at,
                    scheduled_at, retry_count, max_retries, parent_task_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    scheduled,
                    retry_count,
                    max_retries,
                    parent_task_id,
                ),
            )
        self._log_event(
            task_id,
            "task.created",
            "system",
            {
                "task_type": task_type.value,
                "scheduled_at": scheduled,
                "retry_count": retry_count,
                "parent_task_id": parent_task_id,
            },
        )
        return task_id

    def retry_task(self, task_id: str) -> dict:
        """Create a fresh queued task from a failed one, within its retry budget."""
        task = self.get_task(task_id)
        if not task:
            raise PolicyViolation("task not found")
        state = TaskState(task["state"])
        if state is not TaskState.FAILED:
            raise InvalidStateTransition(f"cannot retry a task in state {state.value}")
        retry_count = int(task.get("retry_count") or 0)
        max_retries = int(task.get("max_retries") or 0)
        if retry_count >= max_retries:
            raise PolicyViolation(
                "retry budget exhausted for this task",
                details={"retry_count": retry_count, "max_retries": max_retries},
            )
        with self.db.connect() as conn:
            existing_child = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = ? ORDER BY created_at ASC LIMIT 1",
                (task_id,),
            ).fetchone()
        if existing_child is not None:
            raise PolicyViolation(
                "a retry task already exists for this task; retry the newest failed task instead",
                details={"existing_task_id": existing_child["id"]},
            )
        scheduled_at = None
        if self.retry_delay_s:
            scheduled_at = format_utc(datetime.now(UTC) + timedelta(seconds=self.retry_delay_s))
        new_task_id = self.create_task(
            int(task["repo_id"]),
            TaskType(task["task_type"]),
            str(task["prompt"]),
            model=task.get("model"),
            scheduled_at=scheduled_at,
            max_retries=max_retries,
            parent_task_id=task_id,
            retry_count=retry_count + 1,
        )
        self._log_event(
            task_id,
            "task.retry_created",
            "user",
            {
                "new_task_id": new_task_id,
                "retry_count": retry_count + 1,
                "scheduled_at": scheduled_at,
            },
        )
        return self.get_task(new_task_id) or {}

    def get_task(self, task_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def _log_event(self, task_id: str, event_type: str, actor: str, detail: dict) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, _now(), event_type, actor, json.dumps(redact_value(detail))),
            )

    def _update_state(
        self, task_id: str, from_state: TaskState, to_state: TaskState, **fields
    ) -> None:
        self.state_machine.transition(from_state, to_state)
        now = _now()
        sets = ["state = ?", "updated_at = ?"]
        vals: list[object] = [to_state.value, now]
        if to_state == TaskState.RUNNING and "started_at" not in fields:
            sets.append("started_at = ?")
            vals.append(now)
        if (
            to_state
            in (
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.COMMITTED,
                TaskState.EXPIRED,
                TaskState.ANALYZED,
                TaskState.EXPLAINED,
            )
            and "finished_at" not in fields
        ):
            sets.append("finished_at = ?")
            vals.append(now)
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.extend([task_id, from_state.value])
        with self.db.connect() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND state = ?",  # noqa: S608
                vals,
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT state FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                actual = str(current["state"]) if current is not None else "missing"
                raise InvalidStateTransition(
                    "task state changed concurrently",
                    details={"expected": from_state.value, "actual": actual},
                )
        self._log_event(
            task_id, f"task.state.{to_state.value}", "system", {"from": from_state.value}
        )

    def _fail(self, task_id: str, from_state: TaskState, message: str, **fields: object) -> str:
        """Persist a redacted, bounded failure message and transition to failed."""
        safe = redact_text(str(message)).strip()[:2000] or "task failed"
        self._update_state(task_id, from_state, TaskState.FAILED, error_message=safe, **fields)
        return safe

    def _start_cancel_watch(self, task_id: str) -> tuple[threading.Event, threading.Event]:
        """Register the cancel event and a cross-process watchdog for a run.

        Returns ``(cancel_event, stop_watch)``; the caller must set
        ``stop_watch`` in its ``finally`` block.
        """
        cancel_event = _register_cancel(task_id)
        task = self.get_task(task_id) or {}
        not_before = str(task.get("started_at") or format_utc(datetime.now(UTC)))
        stop_watch = threading.Event()
        watchdog = threading.Thread(
            target=_watch_cancel_requests,
            args=(self.db, task_id, cancel_event, not_before, stop_watch),
            name=f"gitbot-cancel-watch-{task_id[:8]}",
            daemon=True,
        )
        watchdog.start()
        return cancel_event, stop_watch

    def _get_repo(self, repo_id: int) -> Repo:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if row is None:
            raise PolicyViolation("repo not found")
        return Repo.from_row(row)

    def _resolve_repo(self, repo: Repo) -> Path:
        """Resolve an allowlisted path, failing closed on any validation error."""
        return resolve_repo_path(
            repo.path,
            allowed_roots=self.allowed_roots,
            forbidden_roots=self.forbidden_roots,
        )

    def run_task(self, task_id: str) -> dict:
        if not _claim_run(task_id):
            raise InvalidStateTransition("task is already running")
        try:
            return self._run_task_impl(task_id)
        finally:
            _release_run(task_id)

    def _run_task_impl(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if not task:
            raise PolicyViolation("task not found")
        if task["state"] != TaskState.QUEUED.value:
            raise InvalidStateTransition(f"cannot run task in state {task['state']}")

        # A task can sit in the queue (or be scheduled) while the repository
        # allowlist is tightened. Re-check the policy at execution time so a
        # previously queued write task cannot outlive a revoked task type.
        try:
            repo = self._get_repo(int(task["repo_id"]))
        except GitBotError as exc:
            message = self._fail(
                task_id,
                TaskState.QUEUED,
                f"repository validation failed: {exc.message}",
            )
            raise PolicyViolation(message) from exc
        task_type = TaskType(task["task_type"])
        if not repo.allows_task_type(task_type):
            message = self._fail(
                task_id,
                TaskState.QUEUED,
                f"task type {task_type.value!r} is no longer enabled for this repository",
            )
            raise PolicyViolation(
                message,
                details={"repo_id": repo.id, "allowed": list(repo.allowed_task_types)},
            )

        if task_type in READ_ONLY_TASK_TYPES:
            # Read-only lanes are sandboxed, so they are allowed in dry-run.
            terminal = TaskState.EXPLAINED if task_type is TaskType.EXPLAIN else TaskState.ANALYZED
            return self._run_analysis(task, terminal_state=terminal, label=task_type.value)

        if self.dry_run and not self.supervised_automation_enabled:
            self._log_event(task_id, "task.blocked.dry_run", "system", {})
            raise PolicyViolation(
                "GitBot is in dry-run mode; enable supervised automation or set "
                "GITBOT_DRY_RUN=false to allow sandboxed repository changes"
            )

        return self._run_change(task)

    def _run_change(self, task: dict) -> dict:
        """Run one code-change task in the disposable sandbox.

        The source repository is only read (``git status`` and ``git archive``).
        No branch, config file, index entry, or worktree file is written to it
        during execution. The reviewed diff comes from the sandbox workspace.
        """
        task_id = str(task["id"])
        repo = self._get_repo(task["repo_id"])
        try:
            resolved = self._resolve_repo(repo)
        except GitBotError as exc:
            self._fail(task_id, TaskState.QUEUED, f"repository validation failed: {exc.message}")
            raise

        git = GitRepo(resolved)
        preflight = git.preflight(task_id)
        if not preflight.ok:
            reasons = "; ".join(preflight.reasons)
            self._fail(task_id, TaskState.QUEUED, f"preflight failed: {reasons}")
            raise PolicyViolation(f"preflight failed: {reasons}")

        branch_name = preflight.status.branch or ""
        self._update_state(
            task_id,
            TaskState.QUEUED,
            TaskState.RUNNING,
            branch_name=branch_name,
        )

        cancel_event, stop_watch = self._start_cancel_watch(task_id)
        try:
            result = run_change(
                resolved,
                task["prompt"],
                allowed_roots=self.allowed_roots,
                forbidden_roots=self.forbidden_roots,
                config=self.opencode.config,
                timeout_s=self.opencode.config.timeout_s,
                cancel_event=cancel_event,
                bwrap=self.sandbox_bwrap,
                test_command=repo.test_command,
                gateway=self.gateway,
            )
            if cancel_event.is_set() and result.classification is RunClassification.SUCCESS:
                result = replace(
                    result,
                    classification=RunClassification.CANCELLED,
                    error_message="cancelled",
                )
        except GitBotError as exc:
            self._fail(
                task_id,
                TaskState.RUNNING,
                f"sandboxed OpenCode invocation failed: {exc.message}",
            )
            raise
        except Exception as exc:  # defensive: never leave the row running
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                f"sandboxed OpenCode invocation failed: {exc}",
            )
            raise PolicyViolation(message) from exc
        finally:
            stop_watch.set()
            _release_cancel(task_id)

        extra: dict[str, object] = {
            "session_id": result.session_id,
            "sandboxed": 1,
            "snapshot_head": result.snapshot_head,
        }
        if result.classification is RunClassification.CANCELLED:
            self._update_state(
                task_id,
                TaskState.RUNNING,
                TaskState.CANCELLED,
                error_message="cancelled",
                **extra,
            )
            return self.get_task(task_id) or {}

        if result.classification is RunClassification.TIMEOUT:
            message = self._fail(task_id, TaskState.RUNNING, "task timed out", **extra)
            raise PolicyViolation(message)

        if result.classification is RunClassification.PERMISSION_DENIED:
            denied = redact_text(json.dumps(result.denied_actions or []))
            self._log_event(task_id, "task.permission_denied", "system", {"denied_actions": denied})
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                "permission denied by the OpenCode policy",
                **extra,
            )
            raise PermissionDenied(message, details={"denied_actions": result.denied_actions})

        if result.classification in (
            RunClassification.PROCESS_ERROR,
            RunClassification.PROTOCOL_ERROR,
        ):
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                f"OpenCode sandbox failed: {result.error_message or 'process error'}",
                output=result.output,
                output_truncated=int(result.output_truncated),
                **extra,
            )
            raise PolicyViolation(message)

        if result.diff_truncated:
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                "generated diff exceeded the review limit; break the change into smaller tasks",
                output=result.output,
                output_truncated=int(result.output_truncated),
                **extra,
            )
            raise PolicyViolation(message)

        if not result.patch.strip():
            # A write task that produced no repository change must never create a
            # review approval; there would be nothing to approve or commit.
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                "the sandboxed run produced no repository change",
                output=result.output,
                output_truncated=int(result.output_truncated),
                **extra,
            )
            raise PolicyViolation(message)

        unsafe_path = patch_touches_unsafe_git_metadata(result.patch)
        if unsafe_path is not None:
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                "the generated patch writes Git metadata or a Git filter attribute "
                f"({unsafe_path}); refusing to review it",
                output=result.output,
                output_truncated=int(result.output_truncated),
                **extra,
            )
            raise PolicyViolation(message, details={"path": unsafe_path})

        diff_hash = compute_diff_hash(result.patch)
        approval = self.approvals.create(
            task_id,
            repo.id,
            ApprovalAction.COMMIT,
            diff_hash,
            [],
            expires_minutes=60,
        )
        self._update_state(
            task_id,
            TaskState.RUNNING,
            TaskState.AWAITING_REVIEW,
            diff_hash=diff_hash,
            diff_patch=result.patch,
            diff_files=json.dumps(list(result.files)),
            diff_truncated=0,
            output=result.output,
            output_truncated=int(result.output_truncated),
            approval_id=approval.id,
            tests_status=result.tests_status,
            tests_output=result.tests_output,
            tests_output_truncated=int(result.tests_output_truncated),
            tests_command=result.tests_command,
            test_exit_code=result.test_exit_code,
            **extra,
        )
        detail = {
            "diff_hash": diff_hash,
            "sandboxed": True,
            "tests_status": result.tests_status,
        }
        self._log_event(task_id, "task.awaiting_review", "system", detail)
        if result.tests_status == "passed":
            self._log_event(task_id, "task.tests.passed", "system", {"sandboxed": True})
        elif result.tests_status in ("failed", "error"):
            self._log_event(
                task_id,
                "task.tests.failed",
                "system",
                {
                    "status": result.tests_status,
                    "exit_code": result.test_exit_code,
                },
            )
        return self.get_task(task_id) or {}

    def _run_analysis(
        self,
        task: dict,
        *,
        terminal_state: TaskState = TaskState.ANALYZED,
        label: str = "analysis",
    ) -> dict:
        """Run one read-only lane inside the sandbox.

        Analysis and explanation never create a branch, diff, approval, or
        commit. The task ends in ``terminal_state`` with a bounded response, or
        fails with a classified reason.
        """
        task_id = str(task["id"])
        repo = self._get_repo(task["repo_id"])
        try:
            resolved = self._resolve_repo(repo)
        except GitBotError as exc:
            self._fail(task_id, TaskState.QUEUED, f"repository validation failed: {exc.message}")
            raise

        git = GitRepo(resolved)
        preflight = git.preflight(task_id)
        if not preflight.ok:
            reasons = "; ".join(preflight.reasons)
            self._fail(task_id, TaskState.QUEUED, f"preflight failed: {reasons}")
            raise PolicyViolation(f"preflight failed: {reasons}")

        self._update_state(task_id, TaskState.QUEUED, TaskState.RUNNING)
        cancel_event, stop_watch = self._start_cancel_watch(task_id)
        try:
            result = run_analysis(
                resolved,
                task["prompt"],
                allowed_roots=self.allowed_roots,
                forbidden_roots=self.forbidden_roots,
                config=self.opencode.config,
                timeout_s=self.opencode.config.timeout_s,
                cancel_event=cancel_event,
                bwrap=self.sandbox_bwrap,
                gateway=self.gateway,
            )
        except GitBotError as exc:
            self._fail(task_id, TaskState.RUNNING, f"{label} failed: {exc.message}")
            raise
        except Exception as exc:  # defensive: never leave the row running
            message = self._fail(task_id, TaskState.RUNNING, f"{label} failed: {exc}")
            raise PolicyViolation(message) from exc
        finally:
            stop_watch.set()
            _release_cancel(task_id)

        extra: dict[str, object] = {
            "session_id": result.session_id,
            "sandboxed": 1,
            "snapshot_head": result.snapshot_head,
        }
        if result.classification is RunClassification.CANCELLED:
            self._update_state(
                task_id,
                TaskState.RUNNING,
                TaskState.CANCELLED,
                error_message="cancelled",
                **extra,
            )
            return self.get_task(task_id) or {}

        if result.classification is RunClassification.TIMEOUT:
            message = self._fail(task_id, TaskState.RUNNING, f"{label} timed out", **extra)
            raise PolicyViolation(message)

        if result.classification is RunClassification.PERMISSION_DENIED:
            denied = redact_text(json.dumps(result.denied_actions or []))
            self._log_event(task_id, "task.permission_denied", "system", {"denied_actions": denied})
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                "permission denied by the OpenCode policy",
                **extra,
            )
            raise PermissionDenied(message, details={"denied_actions": result.denied_actions})

        if result.classification in (
            RunClassification.PROCESS_ERROR,
            RunClassification.PROTOCOL_ERROR,
        ):
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                f"OpenCode {label} failed: {result.error_message or 'process error'}",
                output=result.output,
                output_truncated=int(result.output_truncated),
                **extra,
            )
            raise PolicyViolation(message)

        if result.tool_calls <= 0:
            # A read-only answer is only trustworthy when the sandboxed agent
            # actually completed a repository tool call.  Without evidence the
            # model answered from assumption, so the task must not be recorded
            # as a completed analysis.
            message = self._fail(
                task_id,
                TaskState.RUNNING,
                f"{label} produced no repository evidence",
                output=result.output,
                output_truncated=int(result.output_truncated),
                **extra,
            )
            raise PolicyViolation(message)

        self._update_state(
            task_id,
            TaskState.RUNNING,
            terminal_state,
            output=result.output,
            output_truncated=int(result.output_truncated),
            **extra,
        )
        self._log_event(
            task_id,
            f"task.{terminal_state.value}",
            "system",
            {
                "snapshot_head": result.snapshot_head,
                "sandboxed": True,
                "duration_ms": result.duration_ms,
            },
        )
        return self.get_task(task_id) or {}

    def commit_task(self, task_id: str) -> dict:
        """Apply the exact sandbox-generated patch and commit it locally.

        The approval is bound to the stored patch hash. Commit refuses when the
        repository HEAD, branch, or working tree changed after review, when the
        stored patch was truncated, or when the patch no longer applies. It
        never pushes and never opens pull requests.
        """
        with _COMMIT_LOCK:
            return self._commit_task_locked(task_id)

    def _commit_task_locked(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if not task:
            raise PolicyViolation("task not found")
        if task["state"] != TaskState.APPROVED.value:
            raise InvalidStateTransition(f"cannot commit task in state {task['state']}")

        repo = self._get_repo(task["repo_id"])
        if not repo.allow_commit:
            raise PolicyViolation(
                "commits are not enabled for this repository",
                details={"repo_id": repo.id},
            )
        if self.dry_run and not (
            self.supervised_automation_enabled and self.allow_approved_local_commits
        ):
            raise PolicyViolation(
                "GitBot is in dry-run mode; refusing to commit. Enable supervised "
                "automation and approved local commits to allow an explicitly approved "
                "local commit while dry-run remains enabled."
            )

        approval_id = task["approval_id"]
        if not approval_id:
            raise ApprovalInvalid("task has no approval record")
        if not task.get("sandboxed") or not task.get("snapshot_head"):
            raise PolicyViolation(
                "this task has no sandboxed review diff; re-run it before committing"
            )
        if task.get("diff_truncated"):
            raise PolicyViolation(
                "the reviewed diff was truncated; re-run the task with a smaller change"
            )
        if task.get("tests_status") in ("failed", "error"):
            raise PolicyViolation(
                "the sandboxed test command did not pass; fix the change before committing",
                details={
                    "tests_status": task.get("tests_status"),
                    "test_exit_code": task.get("test_exit_code"),
                },
            )

        patch = task.get("diff_patch") or ""
        if not patch.strip():
            raise PolicyViolation("there is nothing to commit for this task")
        stored_hash = compute_diff_hash(patch)
        if not task["diff_hash"] or stored_hash != task["diff_hash"]:
            raise ApprovalMismatch("the stored review diff changed; re-run the task")
        # Pre-flight the approval before touching the repository. Failures
        # raised here leave the task approved and the working tree untouched,
        # so an expired or consumed approval never destroys staged work.
        approval = self.approvals.get(approval_id)
        if approval.used_at is not None:
            raise ApprovalInvalid("approval already used")
        try:
            expires_at = parse_iso_utc(approval.expires_at)
        except (ValueError, TypeError) as exc:
            raise ApprovalInvalid("invalid approval expiry") from exc
        if datetime.now(UTC) >= expires_at:
            raise ApprovalExpired("approval expired; re-approve the diff and commit again")
        if approval.task_id != task_id or approval.repo_id != repo.id:
            raise ApprovalMismatch("approval does not match this task or repository")
        if approval.action != ApprovalAction.COMMIT:
            raise ApprovalMismatch("approval action does not match the commit operation")
        if approval.diff_hash != stored_hash:
            raise ApprovalMismatch("the approval does not match the stored review diff")
        unsafe_path = patch_touches_unsafe_git_metadata(patch)
        if unsafe_path is not None:
            raise PolicyViolation(
                "the reviewed patch writes Git metadata or a Git filter attribute",
                details={"path": unsafe_path},
            )

        resolved = self._resolve_repo(repo)
        git = GitRepo(resolved)
        status = git.status()
        if not status.clean:
            raise ApprovalMismatch("the working tree changed since review; re-run the task")
        if task["branch_name"] and status.branch != task["branch_name"]:
            raise PolicyViolation(
                "working tree is not on the reviewed branch",
                details={"expected": task["branch_name"], "actual": status.branch},
            )
        if status.head != task["snapshot_head"]:
            raise ApprovalMismatch("the repository HEAD changed since review; re-run the task")

        with tempfile.TemporaryDirectory(prefix="gitbot-patch-") as temp_dir:
            patch_file = Path(temp_dir) / "review.patch"
            patch_file.write_text(patch, encoding="utf-8")
            os.chmod(patch_file, 0o600)
            try:
                git.run(
                    [
                        "apply",
                        "--check",
                        "--binary",
                        "--whitespace=nowarn",
                        str(patch_file),
                    ]
                )
            except Exception as exc:
                raise ApprovalMismatch(
                    "the reviewed patch no longer applies cleanly; re-run the task"
                ) from exc

            # Journal the commit before the first mutation. If the server dies
            # anywhere after this point, startup recovery fails the task and
            # revokes the approval instead of leaving an invisible half-commit.
            self._log_event(
                task_id,
                "task.commit_started",
                "system",
                {
                    "approval_id": approval_id,
                    "diff_hash": stored_hash,
                    "snapshot_head": task["snapshot_head"],
                },
            )

            applied = False
            try:
                git.run(
                    [
                        "apply",
                        "--index",
                        "--binary",
                        "--whitespace=nowarn",
                        str(patch_file),
                    ]
                )
                applied = True
                staged = capture_staged_diff(resolved, stage=False)
                if compute_diff_hash(staged.patch) != stored_hash:
                    raise ApprovalMismatch(
                        "the applied diff does not match the reviewed diff; "
                        "inspect the working tree"
                    )
                # Consume the single-use approval only once the exact reviewed
                # patch is staged. A crash before this line leaves the approval
                # unused; a crash after it can never be replayed.
                self.approvals.consume(
                    approval_id,
                    task_id=task_id,
                    repo_id=repo.id,
                    diff_hash=stored_hash,
                    action=ApprovalAction.COMMIT,
                )
                # Re-check HEAD right before the commit: the parent of the
                # new commit must be the exact revision that was reviewed.
                head_now = git.status().head
                if head_now != task["snapshot_head"]:
                    raise ApprovalMismatch(
                        "the repository HEAD changed while the commit was being prepared; "
                        "the reviewed patch was rolled back and the task was failed"
                    )
                git.run(["commit", "-m", f"gitbot: {task['task_type']} task {task_id}"])
                applied = False
            except Exception as exc:
                rollback_error = ""
                if applied:
                    # The working tree was clean before this patch, so reverse
                    # applying our own patch only removes GitBot's change. It
                    # never touches pre-existing user work.
                    try:
                        git.run(
                            [
                                "apply",
                                "--reverse",
                                "--index",
                                "--binary",
                                "--whitespace=nowarn",
                                str(patch_file),
                            ]
                        )
                    except Exception as rollback_exc:
                        rollback_error = f"; automatic rollback also failed: {rollback_exc}"
                with suppress(Exception):  # durable recovery still applies
                    self.approvals.revoke(approval_id)
                with suppress(Exception):  # never mask the commit failure
                    self._log_event(
                        task_id,
                        "task.commit_failed",
                        "system",
                        {"approval_id": approval_id, "rolled_back": applied},
                    )
                message = f"commit failed: {exc}{rollback_error}"
                self._fail(task_id, TaskState.APPROVED, message)
                raise

        head = git.status().head
        self._update_state(task_id, TaskState.APPROVED, TaskState.COMMITTED)
        self._log_event(
            task_id,
            "task.committed",
            "system",
            {
                "branch": status.branch,
                "head": head,
                "approval_id": approval_id,
                "diff_hash": stored_hash,
                "sandboxed": True,
            },
        )
        return self.get_task(task_id) or {}

    def cancel_task(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if not task:
            raise PolicyViolation("task not found")
        state = TaskState(task["state"])

        if state in (TaskState.QUEUED, TaskState.AWAITING_REVIEW, TaskState.APPROVED):
            approval_id = task.get("approval_id")
            if state is TaskState.APPROVED:
                # Commit also takes this lock: cancel must not revoke an approval
                # while a local commit is applying the bound patch.
                with _COMMIT_LOCK:
                    return self._cancel_locked(task_id, state, approval_id)
            return self._cancel_locked(task_id, state, approval_id)

        if state == TaskState.RUNNING:
            if request_cancel(task_id):
                self._log_event(task_id, "task.cancel_requested", "user", {})
                return self.get_task(task_id) or {}
            # Not executing in this process. The owning process watches the
            # database, so a durable cancel request reaches it across process
            # boundaries. Without an owner (e.g. a crashed run that was not yet
            # recovered) the request is simply inert.
            started = str(task.get("started_at") or "")
            with self.db.connect() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? "
                    "AND event_type = 'task.cancel_requested' AND created_at >= ? "
                    "LIMIT 1",
                    (task_id, started),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                        "VALUES (?, ?, 'task.cancel_requested', 'user', ?)",
                        (task_id, _now(), json.dumps({"cross_process": True})),
                    )
            return self.get_task(task_id) or {}

        raise InvalidStateTransition(f"cannot cancel task in state {state.value}")

    def _cancel_locked(self, task_id: str, previous: TaskState, approval_id: str | None) -> dict:
        """Cancel and revoke the review approval in one guarded transaction.

        The guarded state update means a concurrent approve/commit either wins
        before cancellation (and cancellation refuses) or sees no approval left
        to consume. The state and approval rows cannot disagree after a crash.
        """
        self.state_machine.transition(previous, TaskState.CANCELLED)
        now = _now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, "
                "finished_at = COALESCE(finished_at, ?) WHERE id = ? AND state = ?",
                (TaskState.CANCELLED.value, now, now, task_id, previous.value),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT state FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                actual = str(current["state"]) if current is not None else "missing"
                raise InvalidStateTransition(
                    "task state changed concurrently",
                    details={"expected": previous.value, "actual": actual},
                )
            if approval_id:
                revoked = conn.execute(
                    "UPDATE approvals SET used_at = COALESCE(used_at, ?) "
                    "WHERE id = ? AND used_at IS NULL",
                    (now, approval_id),
                )
                if revoked.rowcount == 1:
                    conn.execute(
                        "INSERT INTO task_events "
                        "(task_id, created_at, event_type, actor, detail) "
                        "VALUES (?, ?, 'task.approval_revoked', 'user', ?)",
                        (
                            task_id,
                            now,
                            json.dumps({"approval_id": approval_id}),
                        ),
                    )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                "VALUES (?, ?, 'task.state.cancelled', 'system', ?)",
                (task_id, now, json.dumps({"from": previous.value})),
            )
        return self.get_task(task_id) or {}
