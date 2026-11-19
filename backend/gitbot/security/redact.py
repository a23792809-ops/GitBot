"""Secret redaction for logs, audit rows, API responses, and stored errors.

The redactor is intentionally conservative: it rewrites credential *shapes* to
``[REDACTED:<kind>]`` and never needs to know a real secret's value. Callers must
not treat redaction as a substitute for never handling secrets in the first place.
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "[REDACTED:{kind}]"

# (kind, pattern, replacement) — replacement may reference groups with \1 etc.
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        REDACTED.format(kind="github_token"),
    ),
    (
        "github_pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
        REDACTED.format(kind="github_pat"),
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        REDACTED.format(kind="aws_access_key"),
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
            r".*?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED.format(kind="private_key"),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
        REDACTED.format(kind="private_key"),
    ),
    (
        "openai_style_key",
        # Covers sk-..., sk-proj-..., sk-or-v1-..., sk-ant-api03-... and
        # similar shapes used by OpenAI, Anthropic, OpenRouter and other
        # providers.  The body must contain at least one dash/underscore or
        # be at least 20 chars so short words like "sk-clean" are untouched.
        re.compile(r"\bsk-(?:[A-Za-z0-9_-]*[-_])?[A-Za-z0-9_-]{16,}\b"),
        REDACTED.format(kind="openai_style_key"),
    ),
    (
        "groq_key",
        re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
        REDACTED.format(kind="groq_key"),
    ),
    (
        "bearer",
        re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]{12,}"),
        r"\1" + REDACTED.format(kind="bearer"),
    ),
    (
        "basic_auth",
        re.compile(r"(?i)\b(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]{8,}"),
        r"\1" + REDACTED.format(kind="basic_auth"),
    ),
    (
        "key_value_secret",
        re.compile(
            r"(?i)\b([\w-]*(?:token|password|passwd|secret|api[_-]?key)[\w-]*)"
            r"(\s*[=:]\s*)"
            r"(?!\[REDACTED)([\"']?)([^\s\"',;]{12,})\3"
        ),
        r"\1\2\3" + REDACTED.format(kind="redacted") + r"\3",
    ),
)


def redact_text(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced."""
    if not text:
        return text
    for _kind, pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(text: str) -> bool:
    """True when ``text`` contains something the redactor would rewrite."""
    return any(pattern.search(text) for _kind, pattern, _repl in _RULES)


def redact_value(value: Any) -> Any:
    """Recursively redact strings, preserving container types.

    Preserving ``tuple`` matters: ``logging`` does ``msg % record.args`` and a
    tuple is positional while a list is a single value (``"%d" % [1]`` raises).
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {redact_text(str(key)): redact_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


class RedactionFilter(logging.Filter):
    """Logging filter that scrubs credential shapes from messages and args.

    The fully formatted record is redacted first, so a credential passed only
    through ``%s`` arguments (for example
    ``logger.info("Authorization: Bearer %s", token)``) is still removed.  Args
    keep their container type for the fallback path so ``%``-formatting keeps
    working; this is what allows uvicorn/fastapi logs to be routed through our
    handlers safely.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # malformed format args must never break logging
            rendered = None
        if rendered is not None:
            redacted = redact_text(rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = ()
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = redact_value(record.args)
        return True
