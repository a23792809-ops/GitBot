"""Read-only GitHub use cases: cached repo listing and account status.

Keeping the cache here (not in the route) means every future caller — scheduler,
CLI, dashboard — shares the same rate-limit-friendly behaviour.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from gitbot.audit import AuditLog
from gitbot.github.client import GhClient
from gitbot.models import (
    AuditOutcome,
    ContributionSummary,
    GithubAccount,
    GithubRepo,
    RateLimit,
)


@dataclass(frozen=True, slots=True)
class GithubStatus:
    account: GithubAccount
    rate_limit: RateLimit

    def to_dict(self) -> dict[str, object]:
        return {
            "account": self.account.to_dict(),
            "rate_limit": self.rate_limit.to_dict(),
            "read_only": True,
        }


class GithubService:
    def __init__(
        self,
        client: GhClient,
        *,
        audit: AuditLog | None = None,
        ttl_s: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._audit = audit
        self._ttl_s = max(0, ttl_s)
        self._clock = clock
        self._cache: tuple[float, list[GithubRepo]] | None = None

    def status(self) -> GithubStatus:
        account = self._client.auth_status()
        rate = self._client.rate_limit()
        if self._audit is not None:
            self._audit.record(
                event_type="github.read",
                action="auth_status",
                actor="user",
                target=account.login,
                outcome=AuditOutcome.SUCCESS,
                detail={"scopes": list(account.scopes), "remaining": rate.remaining},
            )
        return GithubStatus(account=account, rate_limit=rate)

    def repos(self, *, limit: int = 100, refresh: bool = False) -> list[GithubRepo]:
        now = self._clock()
        if not refresh and self._cache is not None:
            fetched_at, cached = self._cache
            if now - fetched_at < self._ttl_s:
                return cached

        repos = self._client.list_repos(limit)
        self._cache = (now, repos)
        if self._audit is not None:
            self._audit.record(
                event_type="github.read",
                action="list_repos",
                actor="user",
                outcome=AuditOutcome.SUCCESS,
                detail={"count": len(repos), "refresh": refresh},
            )
        return repos

    def invalidate_cache(self) -> None:
        self._cache = None

    def contributions(self, *, from_date: str, to_date: str) -> ContributionSummary:
        """Return contribution activity for the authenticated account."""
        account = self._client.auth_status()
        summary = self._client.contributions(account.login, from_date, to_date)
        if self._audit is not None:
            self._audit.record(
                event_type="github.read",
                action="contributions",
                actor="user",
                target=account.login,
                outcome=AuditOutcome.SUCCESS,
                detail={"from": from_date, "to": to_date, "total": summary.total},
            )
        return summary
