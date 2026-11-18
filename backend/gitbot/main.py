"""CLI entrypoint for GitBot."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from gitbot import __version__
from gitbot.api.app import create_app
from gitbot.audit import AuditLog
from gitbot.config import Settings, load_settings
from gitbot.db import Database
from gitbot.errors import ConfigError
from gitbot.github.client import GhClient
from gitbot.github.service import GithubService
from gitbot.security import ensure_token


def _configure_logging(settings: Settings) -> None:
    from gitbot.logging_setup import configure_logging

    # The resolved GITBOT_LOG_LEVEL must actually reach the root logger; the
    # previous call used the INFO default and silently ignored configuration.
    configure_logging(level=settings.log_level)


def _run_check(settings: Settings) -> int:
    from gitbot.security.sandbox import detect_sandbox_capabilities

    database = Database(settings.db_path)
    applied = database.migrate()
    sandbox = detect_sandbox_capabilities()
    print(
        "ok: config valid "
        f"(bind={settings.host}:{settings.port}, dry_run={settings.dry_run}, "
        f"supervised_automation={settings.supervised_automation_enabled}, "
        f"allow_approved_local_commits={settings.allow_approved_local_commits})"
    )
    print(f"migrations: {', '.join(applied) if applied else 'none'}")
    print(f"database: {settings.db_path}")
    if sandbox.can_run:
        print(f"sandbox: available ({sandbox.version or 'bubblewrap'})")
    else:
        print(f"sandbox: unavailable ({sandbox.detail or 'no strong sandbox'})")
    return 0


def _rotate_token(settings: Settings) -> int:
    token_path = settings.token_file
    from gitbot.security import rotate_token

    rotate_token(token_path)
    print(f"rotated token written to {token_path}")
    return 0


def _find_env_file() -> Path | None:
    """Prefer a project-local .env, then the per-user config directory."""
    for candidate in (Path.cwd() / ".env", Path.home() / ".config" / "gitbot" / ".env"):
        if candidate.is_file():
            return candidate
    return None


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

    overrides = {
        key: value for key, value in (("host", args.host), ("port", args.port)) if value is not None
    }
    try:
        # Overrides go through the same validation as environment values, so
        # `--host 0.0.0.0` is refused rather than quietly binding publicly.
        settings = load_settings(env=os.environ, env_file=_find_env_file(), overrides=overrides)
    except ConfigError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2

    if args.rotate_token:
        return _rotate_token(settings)
    if args.check:
        return _run_check(settings)
    if args.serve or (
        len(sys.argv) == 1 and not any([args.check, args.rotate_token, args.version])
    ):
        # default serve
        pass

    _configure_logging(settings)
    logger = logging.getLogger("gitbot")
    database = Database(settings.db_path)
    database.migrate()
    audit = AuditLog(database)
    ensure_token(settings.token_file)

    client = GhClient(timeout_s=settings.gh_timeout_s)
    github = GithubService(client, audit=audit, ttl_s=settings.github_cache_ttl_s)
    app = create_app(settings=settings, database=database, github=github, audit=audit)

    logger.info("starting GitBot %s", __version__)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
