"""Shared FastAPI dependencies (authentication, state)."""

from __future__ import annotations

from fastapi import Request

from gitbot.api.state import StateDep
from gitbot.errors import UnauthorizedError
from gitbot.security.tokens import read_token, tokens_match

AUTH_HINT = "Send 'Authorization: Bearer <token>' using the token file at ~/.config/gitbot/token"


def require_token(request: Request, state: StateDep) -> None:
    """Bearer-token gate for every mutating endpoint."""
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    supplied = supplied.strip()

    if scheme.lower() != "bearer" or not supplied:
        raise UnauthorizedError("missing bearer token", details={"hint": AUTH_HINT})

    expected = read_token(state.settings.token_file)
    if expected is None:
        raise UnauthorizedError(
            "API token has not been created yet",
            details={"hint": "run `gitbot --rotate-token` to create one"},
        )

    if not tokens_match(expected, supplied):
        raise UnauthorizedError("invalid API token", details={"hint": AUTH_HINT})
