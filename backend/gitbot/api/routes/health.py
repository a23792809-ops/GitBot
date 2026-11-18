"""Health and readiness endpoints."""

from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Depends

from gitbot import __version__
from gitbot.api.deps import require_token
from gitbot.api.state import StateDep
from gitbot.opencode.adapter import probe_opencode
from gitbot.security.sandbox import detect_sandbox_capabilities

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/auth/check", dependencies=[Depends(require_token)])
def auth_check() -> dict[str, Any]:
    """Validate the bearer token without mutating anything.

    The dashboard uses this before enabling write actions, so a mistyped token
    is rejected immediately instead of surfacing on the first mutation.
    """
    return {"authenticated": True, "local_only": True}


@router.get("/health")
def health(state: StateDep) -> dict[str, Any]:
    migrations = state.database.applied_migrations()
    sandbox = detect_sandbox_capabilities()
    opencode = probe_opencode()
    gateway_host_runner = shutil.which("opencode") is not None
    gateway_configured = bool(state.settings.ai_gateway_enabled)
    return {
        "status": "ok",
        "version": __version__,
        "dry_run": state.settings.dry_run,
        "supervised_automation_enabled": state.settings.supervised_automation_enabled,
        "allow_approved_local_commits": state.settings.allow_approved_local_commits,
        "local_only": True,
        "database": {
            "path": str(state.database.path),
            "migrations": [migration.version for migration in migrations],
        },
        "allowlisted_repos": state.repos.count(),
        "sandbox": sandbox.to_dict(),
        "opencode": opencode,
        "ai": {
            # ``ready`` is deliberately conservative: it only means the
            # toolchain, sandbox, and gateway prerequisites are present.  It is
            # not proof that a model provider can be reached.
            "ready": bool(
                sandbox.can_run
                and opencode.get("available")
                and gateway_configured
                and gateway_host_runner
            ),
            "toolchain_ready": bool(sandbox.can_run and opencode.get("available")),
            "provider_verified": False,
            "sandbox_network_isolated": bool(sandbox.can_run and sandbox.network_namespaces),
            "gateway": {
                "enabled": gateway_configured,
                "model": state.settings.ai_gateway_model,
                "host_runner_available": gateway_host_runner,
                "transport": "loopback-relay-to-authenticated-unix-socket",
                "note": (
                    "The sandbox has no direct provider network. The host model runner "
                    "is isolated and allowed only through an allowlisted CONNECT tunnel."
                ),
            },
            "note": (
                "The sandbox, OpenCode binary, policy checks, and host gateway runner "
                "are available. GitBot does not claim provider verification from health; "
                "run a real task to prove the route."
            ),
        },
        "scheduler": state.scheduler.status(),
    }
