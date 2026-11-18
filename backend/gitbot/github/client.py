"""Read-only GitHub access through the authenticated ``gh`` CLI.

Deliberately narrow:
- Only ``gh api`` and ``gh repo list`` reads are exposed here.
- The environment is scrubbed of token variables so ``gh`` uses its own
  keyring/config auth exactly as the user set it up.
- No shell is involved; argv is passed as a list.
- stderr is redacted before it can reach an exception, a log, or the API.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gitbot.errors import GhNotAuthenticated, UpstreamError, UpstreamTimeout
from gitbot.models import (
    ContributionDay,
    ContributionSummary,
    GithubAccount,
    GithubRepo,
    RateLimit,
)
from gitbot.security import redact_text

TOKEN_ENV_VARS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    }
)

AUTH_FAILURE_MARKERS = (
    "not logged in",
    "gh auth login",
    "authentication failed",
    "no oauth token",
    "http 401",
    "bad credentials",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def parse_oauth_scopes(headers_text: str) -> tuple[str, ...]:
    """Extract scopes from ``gh api -i`` response headers."""
    for line in headers_text.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-oauth-scopes":
            return tuple(part.strip() for part in value.split(",") if part.strip())
    return ()


def _scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    for key in TOKEN_ENV_VARS:
        environment.pop(key, None)
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["GH_NO_UPDATE_NOTIFIER"] = "1"
    environment["NO_COLOR"] = "1"
    return environment


class GhClient:
    """Thin, typed wrapper around the ``gh`` CLI."""

    def __init__(
        self,
        executable: str = "gh",
        *,
        timeout_s: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_s = timeout_s
        self._env = _scrubbed_env(env)

    # -- process plumbing ------------------------------------------------
    def run(self, args: Sequence[str], *, timeout_s: float | None = None) -> CommandResult:
        argv = (self.executable, *args)
        started = time.monotonic()
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                list(argv),
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=self._env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise UpstreamError(
                f"{self.executable!r} was not found on PATH",
                details={"executable": self.executable},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise UpstreamTimeout(
                f"gh command timed out after {effective_timeout:g}s",
                details={"argv": list(argv[1:3])},
            ) from exc

        return CommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _raise_for(self, result: CommandResult) -> None:
        stderr = redact_text(result.stderr).strip()
        lowered = stderr.lower()
        subcommand = " ".join(result.argv[1:3])
        if any(marker in lowered for marker in AUTH_FAILURE_MARKERS):
            raise GhNotAuthenticated(
                "GitHub CLI is not authenticated. Run `gh auth login`; "
                "GitBot never handles tokens.",
                details={"command": subcommand},
            )
        raise UpstreamError(
            f"gh {subcommand} failed with exit code {result.returncode}",
            details={
                "command": subcommand,
                "exit_code": result.returncode,
                "stderr": stderr[-800:],
            },
        )

    # -- reads -----------------------------------------------------------
    def api(self, path: str) -> str:
        result = self.run(["api", path])
        if not result.ok:
            self._raise_for(result)
        return result.stdout

    def api_json(self, path: str) -> Any:
        raw = self.api(path)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                f"gh api {path} returned invalid JSON",
                details={"path": path},
            ) from exc

    def auth_status(self) -> GithubAccount:
        payload = self.api_json("user")
        scopes: tuple[str, ...] = ()
        headers = self.run(["api", "-i", "user"])
        if headers.ok:
            scopes = parse_oauth_scopes(headers.stdout)
        return GithubAccount(
            login=str(payload.get("login", "")),
            name=payload.get("name"),
            scopes=scopes,
            authenticated=True,
        )

    def list_repos(self, limit: int = 100) -> list[GithubRepo]:
        bounded = max(1, min(limit, 500))
        payload = self.api_json(
            "user/repos?per_page="
            f"{bounded}&sort=pushed&affiliation=owner,collaborator,organization_member"
        )
        if not isinstance(payload, list):
            raise UpstreamError("unexpected gh api user/repos payload")
        repos: list[GithubRepo] = []
        for item in payload:
            repos.append(_repo_from_api(item))
        return repos

    def rate_limit(self) -> RateLimit:
        payload = self.api_json("rate_limit")
        rate = payload.get("rate", payload) if isinstance(payload, dict) else {}
        return RateLimit(
            limit=int(rate.get("limit", 0)),
            remaining=int(rate.get("remaining", 0)),
            reset_at=int(rate.get("reset", 0)),
            used=int(rate.get("used", 0)),
        )

    def graphql(self, query: str, variables: Mapping[str, str]) -> Any:
        """Run one read-only GraphQL query through ``gh api graphql``."""
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args.extend(["-f", f"{key}={value}"])
        result = self.run(args)
        if not result.ok:
            self._raise_for(result)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UpstreamError("gh api graphql returned invalid JSON") from exc
        if isinstance(payload, dict) and payload.get("errors"):
            messages = [
                str(item.get("message", "graphql error"))
                for item in payload["errors"]
                if isinstance(item, dict)
            ]
            raise UpstreamError(
                "GitHub GraphQL query failed",
                details={"errors": messages[:5]},
            )
        return payload

    def contributions(self, login: str, from_date: str, to_date: str) -> ContributionSummary:
        """Read the contribution calendar and breakdown for one user.

        This is a read-only query. It deliberately does not create commits or
        otherwise attempt to influence contribution activity.
        """
        query = (
            "query($login:String!,$from:DateTime!,$to:DateTime!){"
            " user(login:$login){"
            "  contributionsCollection(from:$from,to:$to){"
            "   totalCommitContributions totalIssueContributions"
            "   totalPullRequestContributions totalPullRequestReviewContributions"
            "   restrictedContributionsCount"
            "   contributionCalendar{ totalContributions weeks{"
            "    contributionDays{ date contributionCount }"
            "   } }"
            "  }"
            " }"
            "}"
        )
        payload = self.graphql(
            query,
            {
                "login": login,
                "from": f"{from_date}T00:00:00Z",
                "to": f"{to_date}T23:59:59Z",
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            raise UpstreamError(
                "GitHub user was not found or contributions are not visible",
                details={"login": login},
            )
        collection = user.get("contributionsCollection") or {}
        calendar = collection.get("contributionCalendar") or {}
        days: list[ContributionDay] = []
        weeks = calendar.get("weeks") or []
        if isinstance(weeks, list):
            for week in weeks:
                if not isinstance(week, dict):
                    continue
                for day in week.get("contributionDays") or []:
                    if not isinstance(day, dict):
                        continue
                    date = str(day.get("date", ""))
                    if not date:
                        continue
                    days.append(
                        ContributionDay(
                            date=date,
                            count=int(day.get("contributionCount", 0) or 0),
                        )
                    )
        days.sort(key=lambda item: item.date)
        return ContributionSummary(
            login=str(user.get("login", login)),
            from_date=from_date,
            to_date=to_date,
            total=int(
                calendar.get("totalContributions", collection.get("totalCommitContributions", 0))
                or 0
            ),
            days=tuple(days),
            commits=int(collection.get("totalCommitContributions", 0) or 0),
            issues=int(collection.get("totalIssueContributions", 0) or 0),
            pull_requests=int(collection.get("totalPullRequestContributions", 0) or 0),
            reviews=int(collection.get("totalPullRequestReviewContributions", 0) or 0),
            restricted=int(collection.get("restrictedContributionsCount", 0) or 0),
        )


def _repo_from_api(item: dict[str, Any]) -> GithubRepo:
    owner = item.get("owner") or {}
    default_branch = item.get("default_branch")
    if default_branch is None and isinstance(item.get("defaultBranchRef"), dict):
        default_branch = item["defaultBranchRef"].get("name")
    return GithubRepo(
        name=str(item.get("name", "")),
        owner=str(owner.get("login", "")) if isinstance(owner, dict) else str(owner),
        visibility=str(item.get("visibility", "private")).lower(),
        default_branch=default_branch,
        is_fork=bool(item.get("fork", item.get("isFork", False))),
        url=str(item.get("html_url", item.get("url", ""))),
        pushed_at=item.get("pushed_at") or item.get("pushedAt"),
    )
