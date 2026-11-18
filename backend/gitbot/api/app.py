"""FastAPI application factory. Bind address is validated in config (loopback only)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gitbot import __version__
from gitbot.api.routes import audit as audit_routes
from gitbot.api.routes import git as git_routes
from gitbot.api.routes import github as github_routes
from gitbot.api.routes import health as health_routes
from gitbot.api.routes import repos as repo_routes
from gitbot.api.routes import schedules as schedules_routes
from gitbot.api.routes import tasks as tasks_routes
from gitbot.api.state import AppState, get_state
from gitbot.audit import AuditLog
from gitbot.config import Settings
from gitbot.db import Database
from gitbot.errors import GitBotError
from gitbot.github.service import GithubService
from gitbot.models import AuditOutcome
from gitbot.opencode.gateway import GatewayConfig
from gitbot.repos import RepoStore
from gitbot.security import redact_text
from gitbot.tasks.engine import recover_stale_tasks
from gitbot.tasks.scheduler import Scheduler

__all__ = ["AppState", "create_app", "get_state"]

logger = logging.getLogger("gitbot.api")


def create_app(
    *,
    settings: Settings,
    database: Database,
    github: GithubService,
    audit: AuditLog | None = None,
    run_migrations: bool = False,
) -> FastAPI:
    audit_log = audit or AuditLog(database)
    scheduler = Scheduler(
        database,
        audit_log,
        dry_run=settings.dry_run,
        supervised_automation_enabled=settings.supervised_automation_enabled,
        allow_approved_local_commits=settings.allow_approved_local_commits,
        home=settings.home,
        interval_s=settings.scheduler_interval_s,
        retry_delay_s=settings.retry_delay_s,
        gateway=GatewayConfig(
            enabled=settings.ai_gateway_enabled,
            model=settings.ai_gateway_model,
            timeout_s=settings.ai_gateway_timeout_s,
            relay_port=settings.ai_gateway_relay_port,
        ),
    )
    state = AppState(
        settings=settings,
        database=database,
        audit=audit_log,
        github=github,
        repos=RepoStore(database),
        scheduler=scheduler,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if run_migrations:
            database.migrate()
        try:
            recovered = recover_stale_tasks(database)
        except Exception:  # startup must report, not crash, on recovery failure
            logger.exception("failed to recover stale running tasks")
        else:
            if recovered:
                audit_log.record(
                    event_type="app.recover_stale",
                    action="recover_stale_tasks",
                    actor="system",
                    outcome=AuditOutcome.INFO,
                    detail={"count": len(recovered), "task_ids": recovered},
                )
                logger.warning("recovered %d stale running task(s)", len(recovered))
        audit_log.record(
            event_type="app.start",
            action="start",
            actor="system",
            target=f"{settings.host}:{settings.port}",
            outcome=AuditOutcome.INFO,
            detail={
                "version": __version__,
                "dry_run": settings.dry_run,
                "supervised_automation_enabled": settings.supervised_automation_enabled,
                "allow_approved_local_commits": settings.allow_approved_local_commits,
            },
        )
        logger.info("GitBot %s listening on %s:%s", __version__, settings.host, settings.port)
        if settings.scheduler_enabled:
            scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(
        title="GitBot",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.gitbot = state

    _register_error_handlers(app)

    app.include_router(health_routes.router)
    app.include_router(github_routes.router)
    app.include_router(repo_routes.router)
    app.include_router(schedules_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(git_routes.router)
    app.include_router(audit_routes.router)

    # Serve static dashboard
    try:
        from pathlib import Path

        static_dir = Path(__file__).parent.parent / "static" / "dashboard"
        if static_dir.exists():

            @app.get("/dashboard")
            async def dashboard() -> FileResponse:
                return FileResponse(static_dir / "index.html")

            app.mount("/static", StaticFiles(directory=str(static_dir.parent)), name="static")
    except Exception:  # noqa: S110
        pass
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GitBotError)
    async def handle_gitbot_error(_: Request, exc: GitBotError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() can carry non-serializable objects (e.g. a ValueError in
        # "ctx"), so only the stable, serializable fields are exposed.
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Log the redacted message only; stack traces go nowhere near the client.
        logger.error(
            "unhandled error on %s %s: %s",
            request.method,
            request.url.path,
            redact_text(str(exc)),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "unexpected server error",
                }
            },
        )
