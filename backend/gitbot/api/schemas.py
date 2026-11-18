"""Request/response schemas for the local API. Extra fields are rejected."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gitbot.models import TaskType

SLUG_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _validate_test_command(value: str | None) -> str | None:
    if value is None:
        return None
    command = value.strip()
    if not command:
        return None
    if len(command) > 500:
        raise ValueError("test_command must be at most 500 characters")
    if any(ord(char) < 32 for char in command):
        raise ValueError("test_command must be a single line without control characters")
    return command


class RepoCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=200)
    default_branch: str = Field(default="main", max_length=200)
    allow_commit: bool = False
    allow_push: bool = False
    allowed_task_types: list[TaskType] | None = None
    test_command: str | None = Field(default=None, max_length=500)

    @field_validator("default_branch")
    @classmethod
    def _branch_is_safe(cls, value: str) -> str:
        if not _SAFE_BRANCH.match(value) or ".." in value or value.endswith("/"):
            raise ValueError("invalid branch name")
        return value

    @field_validator("allow_push")
    @classmethod
    def _push_is_not_implemented(cls, value: bool) -> bool:
        # Push and pull-request creation are deliberately unimplemented. Storing
        # a truthy flag would imply a capability GitBot does not have.
        if value:
            raise ValueError("push is not implemented and cannot be enabled")
        return value

    @field_validator("allowed_task_types")
    @classmethod
    def _types_are_known(cls, value: list[TaskType] | None) -> list[TaskType] | None:
        if value is not None and not value:
            raise ValueError("at least one task type must be allowed")
        return value

    @field_validator("test_command")
    @classmethod
    def _command_is_safe(cls, value: str | None) -> str | None:
        return _validate_test_command(value)


class RepoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_commit: bool | None = None
    allow_push: bool | None = None
    allowed_task_types: list[TaskType] | None = None
    test_command: str | None = Field(default=None, max_length=500)

    @field_validator("allow_push")
    @classmethod
    def _push_is_not_implemented(cls, value: bool | None) -> bool | None:
        if value:
            raise ValueError("push is not implemented and cannot be enabled")
        return value

    @field_validator("allowed_task_types")
    @classmethod
    def _types_are_known(cls, value: list[TaskType] | None) -> list[TaskType] | None:
        if value is not None and not value:
            raise ValueError("at least one task type must be allowed")
        return value

    @field_validator("test_command")
    @classmethod
    def _command_is_safe(cls, value: str | None) -> str | None:
        return _validate_test_command(value)


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: int = Field(ge=1)
    task_type: TaskType
    prompt: str = Field(min_length=1, max_length=32000)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = Field(default=None, max_length=100)
    enabled: bool = True

    @field_validator("prompt")
    @classmethod
    def _prompt_not_whitespace_only(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("schedule prompt cannot be empty")
        return trimmed

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        from gitbot.tasks.schedules import timezone_is_valid

        if not timezone_is_valid(candidate):
            raise ValueError(f"invalid or unsupported timezone: {candidate}")
        return candidate


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: TaskType | None = None
    prompt: str | None = Field(default=None, min_length=1, max_length=32000)
    local_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt_not_whitespace_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("schedule prompt cannot be empty")
        return trimmed

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        from gitbot.tasks.schedules import timezone_is_valid

        if not timezone_is_valid(candidate):
            raise ValueError(f"invalid or unsupported timezone: {candidate}")
        return candidate
