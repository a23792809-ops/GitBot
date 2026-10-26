"""Entry point: `python -m gitbot.main` or the `gitbot` console script.

The bind address comes only from validated configuration, which refuses anything
other than loopback.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from gitbot import __version__
from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.logging_setup import configure_logging


def build_application(settings: Settings) -> object:
    """Wire the object graph. Migrations run here so failures are explicit."""
    settings.ensure_directories()
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    client = GhClient(timeout_s=settings.gh_timeout_s)
    github = GithubService(client, audit=audit, ttl_s=settings.github_cache_ttl_s)
    return create_app(settings=settings, database=database, github=github, audit=audit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitbot", description="GitBot local service")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration, run migrations, then exit without serving",
    )
    parser.add_argument("--version", action="version", version=f"gitbot {__version__}")
    args = parser.parse_args(argv)

    settings = load_settings(env_file=Path(".env"))
    configure_logging(settings.log_level, log_file=settings.data_dir / "gitbot.log")
    app = build_application(settings)

    if args.check:
        print(f"ok: config valid, migrations applied, bind={settings.host}:{settings.port}")
        return 0

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
