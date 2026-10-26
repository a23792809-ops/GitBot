"""Local API token: creation, rotation, and constant-time comparison.

The token authenticates the dashboard to the local API. It never appears in
logs, audit details, or API responses, and the file is 0600 inside a 0700 dir.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from gitbot.errors import ConfigError

TOKEN_BYTES = 32
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 512


class TokenFileError(ConfigError):
    code = "token_file_error"


def _check_permissions(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise TokenFileError(
            f"token file {path} is readable by other users (mode {mode:o}); run `chmod 600 {path}`",
            details={"path": str(path), "mode": oct(mode)},
        )


def read_token(path: Path) -> str | None:
    """Return the stored token, or None when it has not been created yet."""
    if not path.is_file():
        return None
    _check_permissions(path)
    token = path.read_text(encoding="utf-8").strip()
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        raise TokenFileError(
            "token file does not contain a plausible token",
            details={"path": str(path)},
        )
    return token


def ensure_token(path: Path) -> str:
    """Create the token file if missing; never overwrite an existing token."""
    existing = read_token(path)
    if existing is not None:
        return existing

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    # Create with 0600 from the start so the secret is never briefly world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return token


def rotate_token(path: Path) -> str:
    """Replace the token atomically, returning the new value."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    temporary.replace(path)
    return token


def tokens_match(expected: str, supplied: str) -> bool:
    """Constant-time comparison; avoids leaking length/prefix via timing."""
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
