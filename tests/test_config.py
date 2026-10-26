"""Configuration tests: precedence, validation, and localhost-only policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitbot.config import DEFAULT_HOST, DEFAULT_PORT, Settings, load_settings
from gitbot.errors import ConfigError


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **extra}


def test_defaults_are_local_and_dry_run(tmp_path: Path) -> None:
    settings = load_settings(env=_env(tmp_path))

    assert settings.host == DEFAULT_HOST
    assert settings.port == DEFAULT_PORT
    assert settings.dry_run is True
    assert settings.db_path == tmp_path / ".local" / "share" / "gitbot" / "gitbot.db"
    assert settings.token_file == tmp_path / ".config" / "gitbot" / "token"
    assert settings.log_level == "INFO"


def test_xdg_directories_are_respected(tmp_path: Path) -> None:
    settings = load_settings(
        env=_env(
            tmp_path,
            XDG_DATA_HOME=str(tmp_path / "data"),
            XDG_CONFIG_HOME=str(tmp_path / "cfg"),
        )
    )

    assert settings.data_dir == tmp_path / "data" / "gitbot"
    assert settings.config_dir == tmp_path / "cfg" / "gitbot"
    assert settings.db_path == tmp_path / "data" / "gitbot" / "gitbot.db"


def test_env_file_is_read_and_env_vars_override_it(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GITBOT_PORT=9000\nGITBOT_DRY_RUN=false\n# comment\n\n", encoding="utf-8")

    from_file = load_settings(env=_env(tmp_path), env_file=env_file)
    assert from_file.port == 9000
    assert from_file.dry_run is False

    overridden = load_settings(env=_env(tmp_path, GITBOT_PORT="9100"), env_file=env_file)
    assert overridden.port == 9100
    assert overridden.dry_run is False


def test_overrides_win_over_environment(tmp_path: Path) -> None:
    settings = load_settings(env=_env(tmp_path, GITBOT_PORT="9200"), overrides={"port": "9300"})
    assert settings.port == 9300


def test_non_loopback_bind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-loopback"):
        load_settings(env=_env(tmp_path, GITBOT_HOST="0.0.0.0"))


def test_public_ip_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-loopback"):
        load_settings(env=_env(tmp_path, GITBOT_HOST="8.8.8.8"))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(tmp_path: Path, host: str) -> None:
    assert load_settings(env=_env(tmp_path, GITBOT_HOST=host)).host == host


def test_invalid_port_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        load_settings(env=_env(tmp_path, GITBOT_PORT="70000"))


def test_invalid_boolean_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="boolean"):
        load_settings(env=_env(tmp_path, GITBOT_DRY_RUN="maybe"))


def test_invalid_log_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings(env=_env(tmp_path, GITBOT_LOG_LEVEL="LOUD"))


def test_tilde_and_variable_expansion(tmp_path: Path) -> None:
    settings = load_settings(
        env=_env(tmp_path, CUSTOM_DIR=str(tmp_path / "custom")),
        overrides={"db_path": "~/nested/state.db"},
    )
    assert settings.db_path == tmp_path / "nested" / "state.db"

    with_var = load_settings(
        env=_env(tmp_path, CUSTOM_DIR=str(tmp_path / "custom")),
        overrides={"db_path": "$CUSTOM_DIR/state.db"},
    )
    assert with_var.db_path == tmp_path / "custom" / "state.db"


def test_ensure_directories_creates_private_dirs(tmp_path: Path) -> None:
    settings = load_settings(env=_env(tmp_path))
    settings.ensure_directories()

    assert settings.data_dir.is_dir()
    assert settings.config_dir.is_dir()
    assert settings.data_dir.stat().st_mode & 0o777 == 0o700


def test_settings_is_immutable(tmp_path: Path) -> None:
    settings = load_settings(env=_env(tmp_path))
    with pytest.raises(AttributeError):
        settings.port = 1234  # type: ignore[misc]


def test_defaults_object_has_no_credentials() -> None:
    settings = Settings()
    assert not hasattr(settings, "token")
    assert "token" not in settings.__slots__
