"""Repository allowlist. Reads are open on loopback; writes require the API token."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from gitbot.api.deps import require_token
from gitbot.api.schemas import RepoCreate, RepoUpdate
from gitbot.api.state import StateDep
from gitbot.errors import GitBotError, NotFoundError
from gitbot.models import AuditOutcome
from gitbot.security.allowlist import default_allowed_roots, resolve_repo_path

router = APIRouter(prefix="/api/repos", tags=["repositories"])

WRITE_DEPENDENCIES = [Depends(require_token)]


@router.get("")
def list_repos(state: StateDep) -> dict[str, Any]:
    repos = state.repos.list()
    return {
        "count": len(repos),
        "repos": [repo.to_dict() for repo in repos],
        "note": "Only allowlisted repositories may be used for tasks.",
    }


@router.post("", status_code=201, dependencies=WRITE_DEPENDENCIES)
def add_repo(payload: RepoCreate, state: StateDep) -> dict[str, Any]:
    try:
        resolved = resolve_repo_path(
            payload.path,
            allowed_roots=default_allowed_roots(state.settings.home),
        )
    except GitBotError as exc:
        state.audit.record(
            event_type="repo.allowlist",
            action="add",
            actor="user",
            target=payload.path,
            outcome=AuditOutcome.DENIED,
            detail={"code": exc.code, "message": exc.message},
        )
        raise

    slug = payload.slug or f"local/{resolved.name}"
    repo = state.repos.add(
        path=str(resolved),
        slug=slug,
        default_branch=payload.default_branch,
        allow_commit=payload.allow_commit,
        allow_push=payload.allow_push,
    )
    state.audit.record(
        event_type="repo.allowlist",
        action="add",
        actor="user",
        target=repo.slug,
        outcome=AuditOutcome.SUCCESS,
        detail={
            "repo_id": repo.id,
            "path": repo.path,
            "allow_commit": repo.allow_commit,
            "allow_push": repo.allow_push,
        },
    )
    return {"repo": repo.to_dict()}


@router.patch("/{repo_id}", dependencies=WRITE_DEPENDENCIES)
def update_repo(repo_id: int, payload: RepoUpdate, state: StateDep) -> dict[str, Any]:
    existing = state.repos.get(repo_id)
    if existing is None:
        raise NotFoundError("repository is not allowlisted", details={"id": repo_id})

    updated = state.repos.update(
        repo_id, allow_commit=payload.allow_commit, allow_push=payload.allow_push
    )
    if updated is None:  # pragma: no cover - row existed above
        raise NotFoundError("repository is not allowlisted", details={"id": repo_id})

    state.audit.record(
        event_type="repo.allowlist",
        action="update",
        actor="user",
        target=updated.slug,
        outcome=AuditOutcome.SUCCESS,
        detail={
            "repo_id": updated.id,
            "allow_commit": updated.allow_commit,
            "allow_push": updated.allow_push,
        },
    )
    return {"repo": updated.to_dict()}


@router.delete("/{repo_id}", status_code=204, dependencies=WRITE_DEPENDENCIES)
def remove_repo(repo_id: int, state: StateDep) -> Response:
    existing = state.repos.get(repo_id)
    if existing is None:
        raise NotFoundError("repository is not allowlisted", details={"id": repo_id})

    state.repos.delete(repo_id)
    state.audit.record(
        event_type="repo.allowlist",
        action="remove",
        actor="user",
        target=existing.slug,
        outcome=AuditOutcome.SUCCESS,
        detail={"repo_id": existing.id, "path": existing.path},
    )
    return Response(status_code=204)
