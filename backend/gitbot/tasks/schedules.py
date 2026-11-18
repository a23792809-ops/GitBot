"""Persistent recurring daily schedules with timezone and DST handling.

A schedule is a *user-configured intent* to create one task per local calendar
day. It does not execute repository content directly: the scheduler creates a
normal task and hands it to the existing task engine, where the same sandbox,
approval, test, and commit gates apply.

Timezone rules:

* the schedule stores an IANA zone name (``Asia/Kolkata``, ``America/New_York``);
* if the machine cannot expose an IANA name, ``localtime`` loads ``/etc/localtime``
  so the zone database still supplies DST rules;
* every due calculation is performed in the schedule's zone, never in a fixed
  UTC offset;
* a nonexistent spring-forward wall time is moved to the first valid minute
  after the gap, and an ambiguous fall-back wall time uses the first occurrence;
* ``last_run_date`` (the schedule-zone calendar date) makes a schedule run at
  most once per local day, including across restarts and repeated ticks.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gitbot.db import Database
from gitbot.errors import NotFoundError, PolicyViolation
from gitbot.models import TaskType
from gitbot.repos import RepoStore
from gitbot.timeutil import format_utc, utc_now_iso

LOCALTIME_ZONE = "localtime"
_LOCAL_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MAX_PROMPT_CHARS = 32_000
_MAX_TIMEZONE_CHARS = 100
_DST_GAP_SEARCH_MINUTES = 180


def _now() -> str:
    return utc_now_iso()


def generate_schedule_id() -> str:
    return f"sch_{secrets.token_urlsafe(16)}"


def normalize_local_time(value: str) -> str:
    text = value.strip()
    if not _LOCAL_TIME_PATTERN.fullmatch(text):
        raise PolicyViolation(
            "local_time must use 24-hour HH:MM format",
            details={"local_time": value[:20]},
        )
    return text


def normalize_prompt(value: str) -> str:
    prompt = value.strip()
    if not prompt:
        raise PolicyViolation("schedule prompt cannot be empty")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise PolicyViolation(f"schedule prompt must be at most {_MAX_PROMPT_CHARS} characters")
    return prompt


def _load_zone(name: str) -> tzinfo:
    if name == LOCALTIME_ZONE:
        path = Path("/etc/localtime")
        with path.open("rb") as handle:
            return ZoneInfo.from_file(handle, key=LOCALTIME_ZONE)
    return ZoneInfo(name)


def timezone_is_valid(name: str) -> bool:
    candidate = name.strip()
    if not candidate or len(candidate) > _MAX_TIMEZONE_CHARS:
        return False
    try:
        _load_zone(candidate)
    except ZoneInfoNotFoundError, OSError, ValueError:
        return False
    return True


def normalize_timezone(name: str) -> str:
    candidate = name.strip()
    if not timezone_is_valid(candidate):
        raise PolicyViolation(
            "timezone must be a known IANA zone name or 'localtime'",
            details={"timezone": candidate[:100]},
        )
    return candidate


def _zone_from_name(name: str) -> tzinfo:
    try:
        return _load_zone(name)
    except (ZoneInfoNotFoundError, OSError, ValueError) as exc:
        raise PolicyViolation(
            "schedule timezone is not available on this machine",
            details={"timezone": name[:100]},
        ) from exc


def detect_local_timezone_name(env: Mapping[str, str] | None = None) -> str:
    """Return the machine's best available timezone identifier.

    ``TZ`` wins so explicit deployments and tests can select a zone. Otherwise
    Debian's ``/etc/timezone`` and the ``/etc/localtime`` symlink are inspected.
    When only the binary zone file exists, ``localtime`` is returned; loading it
    through ``ZoneInfo.from_file`` preserves DST rules.
    """
    environment = os.environ if env is None else env
    configured = environment.get("TZ", "").strip().lstrip(":")
    if configured and timezone_is_valid(configured):
        return configured

    try:
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text and timezone_is_valid(text):
        return text

    try:
        target = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        target = None
    if target is not None and "zoneinfo" in target.parts:
        index = target.parts.index("zoneinfo")
        candidate = "/".join(target.parts[index + 1 :])
        if candidate and timezone_is_valid(candidate):
            return candidate
    if Path("/etc/localtime").is_file():
        return LOCALTIME_ZONE
    return "UTC"


def _resolve_wall_time(day: date, local_time: str, zone: tzinfo) -> datetime:
    """Resolve a local wall-clock time, handling DST gaps and fold ambiguity."""
    parsed = time.fromisoformat(local_time)
    naive = datetime.combine(day, parsed)
    candidate = naive.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip == naive:
        return candidate

    # Spring-forward gap: the requested wall time does not exist. Prefer the
    # same minute one hour later (e.g. 02:30 -> 03:30) when that is valid.
    jumped = naive + timedelta(hours=1)
    shifted = jumped.replace(tzinfo=zone, fold=0)
    if shifted.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == jumped:
        return shifted

    # Fallback: advance minute-by-minute to the first valid local time.
    probe = naive
    for _ in range(_DST_GAP_SEARCH_MINUTES):
        probe += timedelta(minutes=1)
        shifted = probe.replace(tzinfo=zone, fold=0)
        if shifted.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == probe:
            return shifted
    return candidate


@dataclass(frozen=True, slots=True)
class Schedule:
    id: str
    repo_id: int
    task_type: TaskType
    prompt: str
    local_time: str
    timezone: str
    enabled: bool
    last_run_at: str | None
    last_run_date: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: object) -> Schedule:
        return cls(
            id=str(row["id"]),  # type: ignore[index]
            repo_id=int(row["repo_id"]),  # type: ignore[index]
            task_type=TaskType(str(row["task_type"])),  # type: ignore[index]
            prompt=str(row["prompt"]),  # type: ignore[index]
            local_time=str(row["local_time"]),  # type: ignore[index]
            timezone=str(row["timezone"]),  # type: ignore[index]
            enabled=bool(row["enabled"]),  # type: ignore[index]
            last_run_at=row["last_run_at"],  # type: ignore[index]
            last_run_date=row["last_run_date"],  # type: ignore[index]
            created_at=str(row["created_at"]),  # type: ignore[index]
            updated_at=str(row["updated_at"]),  # type: ignore[index]
        )

    def to_dict(self, *, now: datetime | None = None) -> dict[str, object]:
        effective_now = now or datetime.now(UTC)
        next_run: str | None = None
        with contextlib.suppress(Exception):
            next_run = next_run_at(self, effective_now)
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "task_type": self.task_type.value,
            "prompt": self.prompt,
            "local_time": self.local_time,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
            "last_run_date": self.last_run_date,
            "next_run_at": next_run,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _instant(value: datetime) -> datetime:
    return value.astimezone(UTC)


def due_occurrence(schedule: Schedule, now: datetime) -> datetime | None:
    """Return the local occurrence that is due now, or ``None``.

    A schedule is due when its local wall-clock time for the current schedule
    date has passed and that local date has not already been claimed. The
    returned value carries the schedule's timezone so callers can derive the
    local calendar date without a UTC-boundary mistake.
    """
    if not schedule.enabled:
        return None
    zone = _zone_from_name(schedule.timezone)
    local_now = now.astimezone(zone)
    today = _resolve_wall_time(local_now.date(), schedule.local_time, zone)
    if _instant(local_now) < _instant(today):
        return None
    if schedule.last_run_date == today.date().isoformat():
        return None
    return today


def next_occurrence(schedule: Schedule, now: datetime) -> datetime | None:
    """Return the next UTC occurrence at or after ``now``."""
    if not schedule.enabled:
        return None
    zone = _zone_from_name(schedule.timezone)
    local_now = now.astimezone(zone)
    today = _resolve_wall_time(local_now.date(), schedule.local_time, zone)
    if _instant(today) >= _instant(local_now):
        return today.astimezone(UTC)
    tomorrow = _resolve_wall_time(local_now.date() + timedelta(days=1), schedule.local_time, zone)
    return tomorrow.astimezone(UTC)


def next_run_at(schedule: Schedule, now: datetime) -> str | None:
    occurrence = next_occurrence(schedule, now)
    return format_utc(occurrence) if occurrence is not None else None


def _initial_last_run_date(local_time: str, timezone: str, now: datetime) -> str | None:
    """Prevent a newly created schedule from running immediately for today."""
    zone = _zone_from_name(timezone)
    local_now = now.astimezone(zone)
    today = _resolve_wall_time(local_now.date(), local_time, zone)
    if local_now > today:
        return today.date().isoformat()
    return None


class ScheduleStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list(
        self,
        *,
        repo_id: int | None = None,
        enabled: bool | None = None,
    ) -> list[Schedule]:
        clauses: list[str] = []
        params: list[object] = []
        if repo_id is not None:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM repo_schedules {where} "  # noqa: S608 - clauses are fixed strings
                "ORDER BY local_time ASC, created_at ASC",
                params,
            ).fetchall()
        return [Schedule.from_row(row) for row in rows]

    def get(self, schedule_id: str) -> Schedule | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM repo_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return Schedule.from_row(row) if row is not None else None

    def count(self, *, repo_id: int | None = None) -> int:
        if repo_id is None:
            with self._db.connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM repo_schedules").fetchone()
            return int(row["n"])
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM repo_schedules WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        return int(row["n"])

    def create(
        self,
        repo_id: int,
        task_type: TaskType | str,
        prompt: str,
        local_time: str,
        *,
        timezone: str | None = None,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> Schedule:
        repo = RepoStore(self._db).get(repo_id)
        if repo is None:
            raise NotFoundError("repository is not allowlisted", details={"repo_id": repo_id})
        try:
            normalized_type = task_type if isinstance(task_type, TaskType) else TaskType(task_type)
        except ValueError as exc:
            raise PolicyViolation("unknown task type") from exc
        if not repo.allows_task_type(normalized_type):
            raise PolicyViolation(
                f"task type {normalized_type.value!r} is disabled for this repository",
                details={"repo_id": repo_id, "allowed": list(repo.allowed_task_types)},
            )

        normalized_prompt = normalize_prompt(prompt)
        normalized_time = normalize_local_time(local_time)
        zone_name = normalize_timezone(timezone or detect_local_timezone_name())
        now_dt = now or datetime.now(UTC)
        last_run_date = _initial_last_run_date(normalized_time, zone_name, now_dt)

        schedule_id = generate_schedule_id()
        timestamp = _now()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_schedules (
                    id, repo_id, task_type, prompt, local_time, timezone, enabled,
                    last_run_at, last_run_date, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    schedule_id,
                    repo_id,
                    normalized_type.value,
                    normalized_prompt,
                    normalized_time,
                    zone_name,
                    int(enabled),
                    last_run_date,
                    timestamp,
                    timestamp,
                ),
            )
        created = self.get(schedule_id)
        if created is None:  # pragma: no cover - insert just happened
            raise PolicyViolation("schedule insert could not be read back")
        return created

    def update(
        self,
        schedule_id: str,
        *,
        task_type: TaskType | str | None = None,
        prompt: str | None = None,
        local_time: str | None = None,
        timezone: str | None = None,
        enabled: bool | None = None,
        now: datetime | None = None,
    ) -> Schedule:
        current = self.get(schedule_id)
        if current is None:
            raise NotFoundError("schedule not found", details={"id": schedule_id})

        fields: dict[str, object] = {}
        if task_type is not None:
            try:
                normalized_type = (
                    task_type if isinstance(task_type, TaskType) else TaskType(task_type)
                )
            except ValueError as exc:
                raise PolicyViolation("unknown task type") from exc
            repo = RepoStore(self._db).get(current.repo_id)
            if repo is None:
                raise NotFoundError(
                    "repository is not allowlisted", details={"repo_id": current.repo_id}
                )
            if not repo.allows_task_type(normalized_type):
                raise PolicyViolation(
                    f"task type {normalized_type.value!r} is disabled for this repository",
                    details={"repo_id": current.repo_id, "allowed": list(repo.allowed_task_types)},
                )
            fields["task_type"] = normalized_type.value
        if prompt is not None:
            fields["prompt"] = normalize_prompt(prompt)
        if local_time is not None:
            fields["local_time"] = normalize_local_time(local_time)
        if timezone is not None:
            fields["timezone"] = normalize_timezone(timezone)
        if enabled is not None:
            fields["enabled"] = int(enabled)

        if not fields:
            return current

        new_time = str(fields.get("local_time", current.local_time))
        new_zone = str(fields.get("timezone", current.timezone))
        if "local_time" in fields or "timezone" in fields:
            now_dt = now or datetime.now(UTC)
            zone = _zone_from_name(new_zone)
            today_key = now_dt.astimezone(zone).date().isoformat()
            if current.last_run_date != today_key:
                # Editing a time that has already passed must wait until the
                # next local day; editing a future time may run today.
                fields["last_run_date"] = _initial_last_run_date(new_time, new_zone, now_dt)
        fields["updated_at"] = _now()

        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._db.connect() as conn:
            conn.execute(
                f"UPDATE repo_schedules SET {assignments} WHERE id = ?",  # noqa: S608
                (*fields.values(), schedule_id),
            )
        updated = self.get(schedule_id)
        if updated is None:  # pragma: no cover - row existed above
            raise NotFoundError("schedule not found", details={"id": schedule_id})
        return updated

    def set_enabled(self, schedule_id: str, enabled: bool) -> Schedule:
        current = self.get(schedule_id)
        if current is None:
            raise NotFoundError("schedule not found", details={"id": schedule_id})
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE repo_schedules SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), _now(), schedule_id),
            )
        updated = self.get(schedule_id)
        if updated is None:  # pragma: no cover
            raise NotFoundError("schedule not found", details={"id": schedule_id})
        return updated

    def delete(self, schedule_id: str) -> bool:
        with self._db.connect() as conn:
            cursor = conn.execute("DELETE FROM repo_schedules WHERE id = ?", (schedule_id,))
            return cursor.rowcount > 0

    def claim_due(
        self,
        schedule: Schedule,
        local_date: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically mark one local date as claimed for this schedule.

        Returns the claim timestamp on success, or ``None`` when another tick
        already claimed the date. The timestamp is the release token used if
        task creation fails before a task exists.
        """
        timestamp = _now()
        run_at = format_utc(now or datetime.now(UTC))
        with self._db.connect() as conn:
            cursor = conn.execute(
                "UPDATE repo_schedules SET last_run_date = ?, last_run_at = ?, updated_at = ? "
                "WHERE id = ? AND enabled = 1 "
                "AND (last_run_date IS NULL OR last_run_date != ?)",
                (local_date, run_at, timestamp, schedule.id, local_date),
            )
            return run_at if cursor.rowcount == 1 else None

    def release_claim(
        self,
        schedule: Schedule,
        claim_token: str,
        local_date: str | None = None,
    ) -> bool:
        """Undo a claim when no task was created (e.g. a transient DB error).

        Both the claim token and the claimed local date must still be on the
        row, so a concurrent re-claim or an edit in between is never rolled
        back.
        """
        claimed_date = local_date or schedule.last_run_date
        with self._db.connect() as conn:
            if claimed_date is None:
                return False
            cursor = conn.execute(
                "UPDATE repo_schedules SET last_run_date = ?, last_run_at = ?, updated_at = ? "
                "WHERE id = ? AND last_run_at = ? AND last_run_date = ?",
                (
                    schedule.last_run_date,
                    schedule.last_run_at,
                    _now(),
                    schedule.id,
                    claim_token,
                    claimed_date,
                ),
            )
            return cursor.rowcount == 1

    def history(self, schedule_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        """Return persistent execution history for a schedule from the audit log and tasks."""
        bounded = max(1, min(limit, 100))
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.created_at, a.event_type, a.action, a.actor, a.outcome, a.detail,
                       t.id AS task_id, t.state AS task_state, t.task_type AS task_type,
                       t.created_at AS task_created_at, t.finished_at AS task_finished_at
                FROM audit_log a
                LEFT JOIN tasks t ON t.id = json_extract(a.detail, '$.task_id')
                WHERE a.target = ? AND a.event_type IN ('scheduler.schedule', 'schedule')
                ORDER BY a.id DESC LIMIT ?
                """,
                (schedule_id, bounded),
            ).fetchall()
        entries: list[dict[str, object]] = []
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except json.JSONDecodeError, TypeError:
                detail = {}
            entries.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "outcome": row["outcome"],
                    "local_date": detail.get("local_date"),
                    "task_id": row["task_id"] or detail.get("task_id"),
                    "task_state": row["task_state"],
                    "task_type": row["task_type"],
                    "task_created_at": row["task_created_at"],
                    "task_finished_at": row["task_finished_at"],
                    "error": detail.get("error"),
                }
            )
        return entries
