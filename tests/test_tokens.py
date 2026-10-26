"""Local API token tests: permissions, rotation, and comparison."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from gitbot.security.tokens import (
    TokenFileError,
    ensure_token,
    read_token,
    rotate_token,
    tokens_match,
)


def test_token_is_created_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "token"
    token = ensure_token(path)

    assert path.is_file()
    assert len(token) >= 32
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert file_mode == 0o600
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert dir_mode == 0o700
    assert read_token(path) == token


def test_ensure_token_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "token"
    first = ensure_token(path)
    second = ensure_token(path)
    assert first == second


def test_rotate_replaces_the_token(tmp_path: Path) -> None:
    path = tmp_path / "token"
    original = ensure_token(path)
    rotated = rotate_token(path)

    assert rotated != original
    assert read_token(path) == rotated
    assert not tokens_match(original, rotated)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_token_reads_as_none(tmp_path: Path) -> None:
    assert read_token(tmp_path / "nope") is None


def test_loose_permissions_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "token"
    ensure_token(path)
    path.chmod(0o644)

    with pytest.raises(TokenFileError, match="chmod 600"):
        read_token(path)


def test_implausibly_short_token_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("short\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(TokenFileError, match="plausible"):
        read_token(path)


def test_tokens_match_is_exact() -> None:
    assert tokens_match("abc123", "abc123")
    assert not tokens_match("abc123", "abc124")
    assert not tokens_match("abc123", "abc1234")
