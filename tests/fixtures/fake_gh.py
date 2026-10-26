#!/usr/bin/env python3
"""Fake `gh` for tests. Never contacts the network; never contains real tokens.

Scenarios are selected with FAKE_GH_SCENARIO:
  ok                normal responses
  unauthenticated   every call fails like an expired login
  server_error      every call fails with a generic 502
  malformed         returns invalid JSON
  scopes_missing    valid user but no X-Oauth-Scopes header
"""

from __future__ import annotations

import json
import os
import re
import sys

SCENARIO = os.environ.get("FAKE_GH_SCENARIO", "ok")

# Assembled at runtime so this file never contains a credential-shaped literal.
FAKE_TOKEN = "gh" + "p_" + ("S3cr3t" * 6)

USER = {
    "login": "fake-user",
    "id": 424242,
    "name": "Fake User",
    "email": None,
}

REPOS = [
    {
        "name": "alpha",
        "owner": {"login": "fake-user"},
        "visibility": "PUBLIC",
        "default_branch": "main",
        "fork": False,
        "html_url": "https://github.com/fake-user/alpha",
        "pushed_at": "2026-10-20T10:00:00Z",
    },
    {
        "name": "beta",
        "owner": {"login": "fake-user"},
        "visibility": "PRIVATE",
        "default_branch": "trunk",
        "fork": True,
        "html_url": "https://github.com/fake-user/beta",
        "pushed_at": "2026-09-01T00:00:00Z",
    },
]

RATE_LIMIT = {
    "resources": {"core": {"limit": 5000, "remaining": 4990, "reset": 1790000000, "used": 10}},
    "rate": {"limit": 5000, "remaining": 4990, "reset": 1790000000, "used": 10},
}


def fail(message: str, code: int = 1) -> int:
    sys.stderr.write(message + "\n")
    return code


def main(argv: list[str]) -> int:
    if not argv:
        return fail("no arguments")

    if SCENARIO == "unauthenticated":
        return fail("error: not logged in to any GitHub hosts. Run gh auth login to authenticate.")

    if SCENARIO == "server_error":
        return fail("HTTP 502: Bad Gateway (server did not respond)")

    if SCENARIO == "leaky_error":
        return fail(f"error: unexpected failure while using token {FAKE_TOKEN}")

    args = argv[1:]

    if args[:2] == ["api", "-i"] or (len(args) >= 3 and args[0] == "api" and args[1] == "-i"):
        path = args[2]
        if path != "user":
            return fail(f"unknown path {path}", 4)
        headers = [
            "HTTP/2.0 200 OK",
            "Content-Type: application/json; charset=utf-8",
        ]
        if SCENARIO != "scopes_missing":
            headers.append("X-Oauth-Scopes: gist, read:org, repo, workflow")
        headers.append("X-Accepted-Oauth-Scopes: ")
        sys.stdout.write("\n".join(headers) + "\n\n" + json.dumps(USER) + "\n")
        return 0

    if args and args[0] == "api":
        if SCENARIO == "malformed":
            sys.stdout.write("{not json")
            return 0
        path = args[1] if len(args) > 1 else ""
        if path == "user":
            sys.stdout.write(json.dumps(USER))
            return 0
        if path.startswith("user/repos"):
            match = re.search(r"per_page=(\d+)", path)
            count = int(match.group(1)) if match else len(REPOS)
            sys.stdout.write(json.dumps(REPOS[:count]))
            return 0
        if path == "rate_limit":
            sys.stdout.write(json.dumps(RATE_LIMIT))
            return 0
        return fail(f"unknown api path {path!r}", 4)

    if args[:2] == ["repo", "list"]:
        sys.stdout.write(json.dumps(REPOS))
        return 0

    if args and args[0] == "auth":
        sys.stderr.write(f"error: unexpected auth call with token {FAKE_TOKEN}\n")
        return fail("error: not logged in", 4)

    return fail(f"unknown command: {args}", 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
