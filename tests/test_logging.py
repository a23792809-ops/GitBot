"""Logging configuration must honour GITBOT_LOG_LEVEL and always redact secrets."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from gitbot.config import load_settings
from gitbot.logging_setup import configure_logging
from gitbot.main import _configure_logging
from gitbot.security import RedactionFilter


@pytest.fixture(autouse=True)
def restore_root_level() -> Iterator[None]:
    root = logging.getLogger()
    previous = root.level
    yield
    root.setLevel(previous)


def test_configure_logging_applies_the_requested_level() -> None:
    configure_logging(level="DEBUG")
    assert logging.getLogger().level == logging.DEBUG

    configure_logging(level="error")
    assert logging.getLogger().level == logging.ERROR


def test_configure_logging_attaches_redaction_to_every_handler() -> None:
    configure_logging(level="INFO")
    handlers = logging.getLogger().handlers
    assert handlers
    for handler in handlers:
        assert any(isinstance(item, RedactionFilter) for item in handler.filters)


def test_configured_logging_redacts_authorization_headers() -> None:
    configure_logging(level="INFO")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("gitbot.test.redaction")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        token = "A" * 43
        logger.warning("request failed with Authorization: Bearer %s", token)
    finally:
        logger.removeHandler(handler)
        handler.close()

    output = stream.getvalue()
    assert token not in output
    assert "[REDACTED:bearer]" in output


def test_cli_logging_uses_the_configured_level(tmp_path: Path) -> None:
    settings = load_settings(env={"HOME": str(tmp_path), "GITBOT_LOG_LEVEL": "WARNING"})
    _configure_logging(settings)
    assert logging.getLogger().level == logging.WARNING
