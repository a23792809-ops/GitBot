"""Health and readiness endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from gitbot import __version__
from gitbot.api.state import StateDep

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(state: StateDep) -> dict[str, Any]:
    migrations = state.database.applied_migrations()
    return {
        "status": "ok",
        "version": __version__,
        "dry_run": state.settings.dry_run,
        "local_only": True,
        "database": {
            "path": str(state.database.path),
            "migrations": [migration.version for migration in migrations],
        },
        "allowlisted_repos": state.repos.count(),
    }
