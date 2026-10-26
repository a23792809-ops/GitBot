"""Application state container shared by the app factory and route modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from gitbot.audit import AuditLog
from gitbot.config import Settings
from gitbot.db import Database
from gitbot.github.service import GithubService
from gitbot.repos import RepoStore


@dataclass(slots=True)
class AppState:
    settings: Settings
    database: Database
    audit: AuditLog
    github: GithubService
    repos: RepoStore


def get_state(request: Request) -> AppState:
    state: Any = request.app.state.gitbot
    if not isinstance(state, AppState):  # pragma: no cover - wiring bug
        raise RuntimeError("app state is not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]
