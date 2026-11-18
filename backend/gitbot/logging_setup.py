"""Logging setup: stdout + optional rotating file, always redaction-filtered."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from gitbot.security import RedactionFilter

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


def _attach_redaction(handler: logging.Handler) -> None:
    if not any(isinstance(item, RedactionFilter) for item in handler.filters):
        handler.addFilter(RedactionFilter())


def configure_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    force: bool = False,
) -> None:
    """Configure root logging with the redaction filter attached to every handler."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    if not root.handlers:
        formatter = logging.Formatter(LOG_FORMAT)
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # Existing handlers may have been installed by uvicorn or a test harness;
    # they must be redaction-filtered too, not just the handler created above.
    for handler in root.handlers:
        _attach_redaction(handler)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        already = any(
            isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == log_file.resolve()
            for handler in root.handlers
        )
        if not already:
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            root.addHandler(file_handler)
        for handler in root.handlers:
            _attach_redaction(handler)
