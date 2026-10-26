"""Domain types shared by the DB, API, and GitHub adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskType(StrEnum):
    ANALYZE = "analyze"
    BUGFIX = "bugfix"
    FEATURE = "feature"
    DOCS = "docs"
    DEPS = "deps"
    TESTS = "tests"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
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
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> Repo:
        return cls(
            id=row["id"],
            path=row["path"],
            slug=row["slug"],
            default_branch=row["default_branch"],
            allow_commit=bool(row["allow_commit"]),
            allow_push=bool(row["allow_push"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "slug": self.slug,
            "default_branch": self.default_branch,
            "allow_commit": self.allow_commit,
            "allow_push": self.allow_push,
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
