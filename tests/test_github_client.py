"""Tests for the read-only `gh` adapter. All runs go through tests/fixtures/fake_gh.py."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from gitbot.errors import GhNotAuthenticated, UpstreamError, UpstreamTimeout
from gitbot.github import GhClient
from gitbot.github.client import _scrubbed_env, parse_oauth_scopes


@pytest.fixture
def gh(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GhClient]:
    monkeypatch.setenv("FAKE_GH_SCENARIO", "ok")
    yield GhClient(str(fake_gh), timeout_s=10)


def _client(fake_gh: Path, scenario: str, monkeypatch: pytest.MonkeyPatch, **kwargs) -> GhClient:
    monkeypatch.setenv("FAKE_GH_SCENARIO", scenario)
    return GhClient(str(fake_gh), **kwargs)


def test_auth_status_parses_login_and_scopes(gh: GhClient) -> None:
    account = gh.auth_status()

    assert account.login == "fake-user"
    assert account.name == "Fake User"
    assert account.authenticated is True
    assert account.scopes == ("gist", "read:org", "repo", "workflow")


def test_auth_status_tolerates_missing_scope_header(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(fake_gh, "scopes_missing", monkeypatch)
    assert client.auth_status().scopes == ()


def test_list_repos_maps_fields(gh: GhClient) -> None:
    repos = gh.list_repos()

    assert [repo.slug for repo in repos] == ["fake-user/alpha", "fake-user/beta"]
    alpha, beta = repos
    assert alpha.visibility == "public"
    assert alpha.default_branch == "main"
    assert alpha.is_fork is False
    assert beta.visibility == "private"
    assert beta.is_fork is True
    assert beta.url.endswith("/beta")


def test_list_repos_limit_is_clamped_and_passed_through(gh: GhClient) -> None:
    assert len(gh.list_repos(limit=1)) == 1
    assert len(gh.list_repos(limit=10_000)) == 2


def test_rate_limit(gh: GhClient) -> None:
    rate = gh.rate_limit()
    assert rate.limit == 5000
    assert rate.remaining == 4990
    assert rate.used == 10


def test_unauthenticated_gh_raises_specific_error(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(fake_gh, "unauthenticated", monkeypatch)
    with pytest.raises(GhNotAuthenticated, match="gh auth login"):
        client.auth_status()


def test_generic_failure_is_upstream_error(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(fake_gh, "server_error", monkeypatch)
    with pytest.raises(UpstreamError) as excinfo:
        client.list_repos()
    assert excinfo.value.code == "upstream_error"
    assert "502" in excinfo.value.details["stderr"]
    assert excinfo.value.details["exit_code"] == 1


def test_stderr_is_redacted_before_it_reaches_the_exception(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(fake_gh, "leaky_error", monkeypatch)
    with pytest.raises(UpstreamError) as excinfo:
        client.list_repos()

    error = excinfo.value
    leak = "gh" + "p_" + ("S3cr3t" * 6)
    assert leak not in str(error)
    assert leak not in repr(error.details)
    assert "[REDACTED:github_token]" in error.details["stderr"]


def test_malformed_json_is_upstream_error(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(fake_gh, "malformed", monkeypatch)
    with pytest.raises(UpstreamError, match="invalid JSON"):
        client.auth_status()


def test_missing_executable_is_upstream_error() -> None:
    client = GhClient("/nonexistent/gh-binary")
    with pytest.raises(UpstreamError, match="not found on PATH"):
        client.auth_status()


def test_timeout_is_reported_as_upstream_timeout() -> None:
    client = GhClient("sleep", timeout_s=0.2)
    with pytest.raises(UpstreamTimeout, match="timed out"):
        client.run(["5"])


def test_token_env_vars_are_scrubbed() -> None:
    env = _scrubbed_env(
        {
            "PATH": "/usr/bin",
            "GH_TOKEN": "x",
            "GITHUB_TOKEN": "y",
            "GH_ENTERPRISE_TOKEN": "z",
            "HOME": "/home/example",
        }
    )
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_ENTERPRISE_TOKEN" not in env
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["PATH"] == "/usr/bin"


def test_parse_oauth_scopes_variants() -> None:
    assert parse_oauth_scopes("X-Oauth-Scopes: repo, gist\n") == ("repo", "gist")
    assert parse_oauth_scopes("x-oauth-scopes:\n") == ()
    assert parse_oauth_scopes("Content-Type: application/json\n") == ()


def test_client_does_not_inherit_token_env_in_real_env(fake_gh: Path) -> None:
    """Even if the parent process has GH_TOKEN set, the child must not see it."""
    client = GhClient(str(fake_gh))
    assert "GH_TOKEN" not in client._env
    assert os.environ.get("GH_TOKEN") in (None, client._env.get("GH_TOKEN"))
