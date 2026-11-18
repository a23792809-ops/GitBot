"""Domain types shared by the DB, API, and GitHub adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskType(StrEnum):
    ANALYZE = "analyze"
    EXPLAIN = "explain"
    BUGFIX = "bugfix"
    FEATURE = "feature"
    DOCS = "docs"
    DEPS = "deps"
    TESTS = "tests"


# Task types that only read the repository. They are allowed while dry-run is
# enabled and never produce a diff, approval, or commit.
READ_ONLY_TASK_TYPES = frozenset({TaskType.ANALYZE, TaskType.EXPLAIN})
ALL_TASK_TYPES: tuple[TaskType, ...] = (
    TaskType.ANALYZE,
    TaskType.EXPLAIN,
    TaskType.BUGFIX,
    TaskType.FEATURE,
    TaskType.DOCS,
    TaskType.DEPS,
    TaskType.TESTS,
)


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    ANALYZED = "analyzed"
    EXPLAINED = "explained"
    APPROVED = "approved"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ApprovalAction(StrEnum):
    COMMIT = "commit"
    PUSH = "push"
    CREATE_PR = "create_pr"
    MERGE = "merge"
    DELETE_REPO = "delete_repo"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Repo:
    """An explicitly allowlisted local repository."""

    id: int
    path: str
    slug: str
    default_branch: str
    allow_commit: bool
    allow_push: bool
    allowed_task_types: tuple[str, ...]
    test_command: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> Repo:
        raw_types = row["allowed_task_types"] or ""
        allowed = tuple(part for part in raw_types.split(",") if part)
        return cls(
            id=row["id"],
            path=row["path"],
            slug=row["slug"],
            default_branch=row["default_branch"],
            allow_commit=bool(row["allow_commit"]),
            allow_push=bool(row["allow_push"]),
            allowed_task_types=allowed,
            test_command=row["test_command"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def allows_task_type(self, task_type: TaskType | str) -> bool:
        value = task_type.value if isinstance(task_type, TaskType) else str(task_type)
        return value in self.allowed_task_types

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "slug": self.slug,
            "default_branch": self.default_branch,
            "allow_commit": self.allow_commit,
            "allow_push": self.allow_push,
            "allowed_task_types": list(self.allowed_task_types),
            "test_command": self.test_command,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class GithubAccount:
    login: str
    name: str | None
    scopes: tuple[str, ...]
    authenticated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "name": self.name,
            "scopes": list(self.scopes),
            "authenticated": self.authenticated,
        }


@dataclass(frozen=True, slots=True)
class GithubRepo:
    """A repository visible to the authenticated gh account (read-only view)."""

    name: str
    owner: str
    visibility: str
    default_branch: str | None
    is_fork: bool
    url: str
    pushed_at: str | None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner": self.owner,
            "slug": self.slug,
            "visibility": self.visibility,
            "default_branch": self.default_branch,
            "is_fork": self.is_fork,
            "url": self.url,
            "pushed_at": self.pushed_at,
        }


@dataclass(frozen=True, slots=True)
class ContributionDay:
    date: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "count": self.count}


@dataclass(frozen=True, slots=True)
class ContributionSummary:
    login: str
    from_date: str
    to_date: str
    total: int
    days: tuple[ContributionDay, ...]
    commits: int = 0
    issues: int = 0
    pull_requests: int = 0
    reviews: int = 0
    restricted: int = 0

    def to_dict(self) -> dict[str, Any]:
        active_days = sum(1 for day in self.days if day.count > 0)
        return {
            "login": self.login,
            "from": self.from_date,
            "to": self.to_date,
            "total": self.total,
            "active_days": active_days,
            "days": [day.to_dict() for day in self.days],
            "breakdown": {
                "commits": self.commits,
                "issues": self.issues,
                "pull_requests": self.pull_requests,
                "reviews": self.reviews,
                "restricted": self.restricted,
            },
            "read_only": True,
            "note": (
                "GitHub attributes contributions to the commit author email and the "
                "repository's default branch. GitBot does not create artificial activity."
            ),
        }


@dataclass(frozen=True, slots=True)
class RateLimit:
    limit: int
    remaining: int
    reset_at: int
    used: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "used": self.used,
        }
