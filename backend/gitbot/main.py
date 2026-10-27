"""CLI entrypoint for GitBot."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from gitbot import __version__
from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.security import ensure_token


def _configure_logging() -> None:
    from gitbot.logging_setup import configure_logging

    configure_logging()


def _run_check(settings: Settings) -> int:
    database = Database(settings.db_path)
    applied = database.migrate()
    print(f"ok: config valid (bind={settings.host}:{settings.port}, dry_run={settings.dry_run})")
    print(f"migrations: {', '.join(applied) if applied else 'none'}")
    print(f"database: {settings.db_path}")
    return 0


def _rotate_token(settings: Settings) -> int:
    token_path = settings.token_file
    from gitbot.security import rotate_token

    rotate_token(token_path)
    print(f"rotated token written to {token_path}")
    return 0


def _recover_stale_running(database: Database, audit: AuditLog) -> None:
    """Mark tasks stuck in running as interrupted on startup."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with database.connect() as conn:
        rows = conn.execute("SELECT id FROM tasks WHERE state = 'running'").fetchall()
        for row in rows:
            try:
                conn.execute(
                    "UPDATE tasks SET state = 'failed', "
                    "error_message = COALESCE(error_message, 'interrupted'), "
                    "finished_at = COALESCE(finished_at, ?), updated_at = ? "
                    "WHERE id = ? AND state = 'running'",
                    (now, now, row["id"]),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, created_at, event_type, actor, detail) "
                    "VALUES (?, ?, 'task.interrupted', 'system', '{}')",
                    (row["id"], now),
                )
            except Exception:  # noqa: S110
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitbot")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rotate-token", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    settings = load_settings(env=os.environ)
    if args.host is not None:
        settings = settings.model_copy(update={"host": args.host})
    if args.port is not None:
        settings = settings.model_copy(update={"port": args.port})

    if args.rotate_token:
        return _rotate_token(settings)
    if args.check:
        return _run_check(settings)
    if args.serve or (
        len(sys.argv) == 1 and not any([args.check, args.rotate_token, args.version])
    ):
        # default serve
        pass

    _configure_logging()
    logger = logging.getLogger("gitbot")
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    _recover_stale_running(database, audit)
    ensure_token(settings.token_file)

    client = GhClient(timeout_s=settings.gh_timeout_s)
    github = GithubService(client, audit=audit, ttl_s=settings.github_cache_ttl_s)
    app = create_app(settings=settings, database=database, github=github, audit=audit)

    logger.info("starting GitBot %s", __version__)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
