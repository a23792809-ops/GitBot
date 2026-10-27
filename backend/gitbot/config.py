"""Configuration loading: defaults < .env file < GITBOT_* environment < overrides.

The dashboard/API is localhost-only by policy; a non-loopback bind is a hard
configuration error rather than a warning.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gitbot.errors import ConfigError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LOG_LEVEL = "INFO"
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_ENV_PREFIX = "GITBOT_"
DEFAULT_HOME = Path.home()


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved runtime settings. Contains no credentials by design."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    dry_run: bool = True
    home: Path = DEFAULT_HOME
    data_dir: Path = DEFAULT_HOME / ".local" / "share" / "gitbot"
    config_dir: Path = DEFAULT_HOME / ".config" / "gitbot"
    db_path: Path = DEFAULT_HOME / ".local" / "share" / "gitbot" / "gitbot.db"
    token_file: Path = DEFAULT_HOME / ".config" / "gitbot" / "token"
    gh_timeout_s: float = 30.0
    github_cache_ttl_s: int = 60

    def ensure_directories(self) -> None:
        """Create data/config directories with 0700 permissions."""
        for directory in (self.data_dir, self.config_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    @property
    def is_dry_run(self) -> bool:
        return self.dry_run

    @property
    def token_path(self) -> Path:
        return self.token_file


def _parse_bool(key: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(
        f"{key} must be a boolean (true/false), got {raw!r}",
        details={"key": key},
    )


def _parse_int(key: str, raw: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}", details={"key": key}) from exc
    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{key} must be between {minimum} and {maximum}, got {value}",
            details={"key": key},
        )
    return value


def _parse_float(key: str, raw: str, *, minimum: float, maximum: float) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}", details={"key": key}) from exc
    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{key} must be between {minimum} and {maximum}, got {value}",
            details={"key": key},
        )
    return value


_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_vars(text: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR`` / ``${VAR}`` using the provided environment only."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))

    return _VAR_PATTERN.sub(replace, text)


def _expand_path(raw: str | Path, env: Mapping[str, str]) -> Path:
    text = os.fspath(raw).strip()
    home = env.get("HOME") or os.path.expanduser("~")
    if text == "~":
        text = home
    elif text.startswith("~/"):
        text = home + text[1:]
    text = _expand_vars(text, env)
    return Path(text)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file. Blank lines and ``#`` comments are ignored."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(
                f"{path}:{lineno}: expected KEY=VALUE",
                details={"file": str(path), "line": lineno},
            )
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError(f"{path}:{lineno}: empty key", details={"file": str(path)})
        values[key] = value.strip().strip("\"'")
    return values


def _require_loopback(host: str) -> str:
    candidate = host.strip()
    if candidate.lower() == "localhost":
        return candidate
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ConfigError(
            f"GITBOT_HOST must be a loopback address or 'localhost', got {host!r}",
            details={"host": host},
        ) from exc
    if not parsed.is_loopback:
        raise ConfigError(
            "refusing to bind the dashboard to a non-loopback address; "
            "GitBot is local-only by policy",
            details={"host": host},
        )
    return candidate


def load_settings(
    *,
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Resolve settings from layered sources.

    ``env`` defaults to ``os.environ``; pass a mapping in tests to stay hermetic.
    """
    environment = dict(os.environ if env is None else env)
    home = _expand_path(environment.get("HOME") or "~", environment)
    data_dir = (
        _expand_path(environment.get("XDG_DATA_HOME", str(home / ".local" / "share")), environment)
        / "gitbot"
    )
    config_dir = (
        _expand_path(environment.get("XDG_CONFIG_HOME", str(home / ".config")), environment)
        / "gitbot"
    )

    values: dict[str, str] = {
        "host": DEFAULT_HOST,
        "port": str(DEFAULT_PORT),
        "log_level": DEFAULT_LOG_LEVEL,
        "dry_run": "true",
        "data_dir": str(data_dir),
        "config_dir": str(config_dir),
        "db_path": str(data_dir / "gitbot.db"),
        "token_file": str(config_dir / "token"),
        "gh_timeout_s": "30",
        "github_cache_ttl_s": "60",
    }

    file_values = read_env_file(env_file) if env_file is not None else {}
    for key, value in file_values.items():
        normalized = key[len(_ENV_PREFIX) :].lower() if key.startswith(_ENV_PREFIX) else key.lower()
        values[normalized] = value

    for key, value in environment.items():
        if key.startswith(_ENV_PREFIX):
            values[key[len(_ENV_PREFIX) :].lower()] = value

    for key, value in (overrides or {}).items():
        values[key.lower()] = os.fspath(value) if isinstance(value, Path) else str(value)

    settings = Settings(
        host=_require_loopback(values["host"]),
        port=_parse_int("GITBOT_PORT", values["port"], minimum=1, maximum=65535),
        log_level=_normalize_log_level(values["log_level"]),
        dry_run=_parse_bool("GITBOT_DRY_RUN", values["dry_run"]),
        home=home,
        data_dir=_expand_path(values["data_dir"], environment),
        config_dir=_expand_path(values["config_dir"], environment),
        db_path=_expand_path(values["db_path"], environment),
        token_file=_expand_path(values["token_file"], environment),
        gh_timeout_s=_parse_float(
            "GITBOT_GH_TIMEOUT_S", values["gh_timeout_s"], minimum=1.0, maximum=300.0
        ),
        github_cache_ttl_s=_parse_int(
            "GITBOT_GITHUB_CACHE_TTL_S", values["github_cache_ttl_s"], minimum=0, maximum=3600
        ),
    )
    return settings


def _normalize_log_level(raw: str) -> str:
    level = raw.strip().upper()
    if level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"GITBOT_LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)}, got {raw!r}",
            details={"log_level": raw},
        )
    return level


def with_overrides(settings: Settings, **changes: Any) -> Settings:
    """Return a copy of ``settings`` with validated replacements (test helper)."""
    return replace(settings, **changes)
