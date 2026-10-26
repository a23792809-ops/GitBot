"""GithubService cache behaviour, tested with a stub client and a fake clock."""

from __future__ import annotations

import pytest

from gitbot.audit import AuditLog
from gitbot.github.service import GithubService
from gitbot.models import GithubAccount, GithubRepo, RateLimit


class StubGh:
    def __init__(self) -> None:
        self.list_calls = 0
        self.status_calls = 0

    def auth_status(self) -> GithubAccount:
        self.status_calls += 1
        return GithubAccount(login="stub", name=None, scopes=("repo",))

    def rate_limit(self) -> RateLimit:
        return RateLimit(limit=5000, remaining=4999, reset_at=0)

    def list_repos(self, limit: int = 100) -> list[GithubRepo]:
        self.list_calls += 1
        return [
            GithubRepo(
                name=f"repo{i}",
                owner="stub",
                visibility="public",
                default_branch="main",
                is_fork=False,
                url=f"https://example.invalid/repo{i}",
                pushed_at=None,
            )
            for i in range(min(limit, 3))
        ]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_repos_are_cached_within_ttl() -> None:
    client = StubGh()
    clock = Clock()
    service = GithubService(client, ttl_s=60, clock=clock)  # type: ignore[arg-type]

    first = service.repos()
    clock.advance(30)
    second = service.repos()

    assert first == second
    assert client.list_calls == 1


def test_cache_expires_after_ttl() -> None:
    client = StubGh()
    clock = Clock()
    service = GithubService(client, ttl_s=60, clock=clock)  # type: ignore[arg-type]

    service.repos()
    clock.advance(61)
    service.repos()

    assert client.list_calls == 2


def test_refresh_bypasses_cache_and_invalidate_clears_it() -> None:
    client = StubGh()
    service = GithubService(client, ttl_s=600)  # type: ignore[arg-type]

    service.repos()
    service.repos(refresh=True)
    assert client.list_calls == 2

    service.invalidate_cache()
    service.repos()
    assert client.list_calls == 3


def test_status_is_never_cached() -> None:
    client = StubGh()
    service = GithubService(client, ttl_s=600)  # type: ignore[arg-type]

    service.status()
    service.status()
    assert client.status_calls == 2


def test_reads_are_audited(audit_log: AuditLog) -> None:
    client = StubGh()
    service = GithubService(client, audit=audit_log, ttl_s=600)  # type: ignore[arg-type]

    service.status()
    service.repos()

    entries = audit_log.list_entries()
    actions = [entry.action for entry in entries]
    assert "auth_status" in actions
    assert "list_repos" in actions
    assert all(entry.event_type == "github.read" for entry in entries)


def test_ttl_zero_disables_caching() -> None:
    client = StubGh()
    service = GithubService(client, ttl_s=0)  # type: ignore[arg-type]

    service.repos()
    service.repos()
    assert client.list_calls == 2


@pytest.mark.parametrize("limit", [1, 2, 100])
def test_limit_is_forwarded(limit: int) -> None:
    client = StubGh()
    service = GithubService(client, ttl_s=0)  # type: ignore[arg-type]
    assert len(service.repos(limit=limit)) == min(limit, 3)
