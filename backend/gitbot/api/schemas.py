"""Request/response schemas for the local API. Extra fields are rejected."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class RepoCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=200)
    default_branch: str = Field(default="main", max_length=200)
    allow_commit: bool = False
    allow_push: bool = False

    @field_validator("default_branch")
    @classmethod
    def _branch_is_safe(cls, value: str) -> str:
        if not _SAFE_BRANCH.match(value) or ".." in value or value.endswith("/"):
            raise ValueError("invalid branch name")
        return value


class RepoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_commit: bool | None = None
    allow_push: bool | None = None
