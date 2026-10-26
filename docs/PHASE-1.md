# Phase 1 — Read-only spine (implementation notes)

Status: **complete**. 98 tests passing, `ruff check` and `ruff format --check` clean.

## What exists now

```
backend/gitbot/
├── config.py            # layered settings; loopback-only bind enforced
├── logging_setup.py     # stdout + rotating file, redaction filter on every handler
├── db.py                # SQLite (WAL) + forward-only checksum-verified migrations
├── schema/0001_init.sql # repos, settings, append-only audit_log
├── models.py            # typed domain objects (Repo, GithubAccount, GithubRepo, ...)
├── audit.py             # append-only audit writer/reader
├── errors.py            # typed error hierarchy → stable API codes
├── repos.py             # allowlist store (reads only in Phase 1)
├── github/client.py     # `gh` subprocess adapter, token-scrubbed env, redacted stderr
├── github/service.py    # cached, audited read-only GitHub use cases
├── api/app.py           # FastAPI factory + error handlers
├── api/state.py         # AppState + StateDep (Annotated dependency)
├── api/routes/*.py      # health, github, repos, audit
└── main.py              # `gitbot` console script / `python -m gitbot.main`
```

## Endpoints (all GET, all localhost)

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | version, dry-run flag, DB path, applied migrations, allowlist count |
| `GET /api/github/status` | gh login, name, OAuth scopes, rate limit |
| `GET /api/github/repos?limit=&refresh=` | repos visible to the account (cached, TTL default 60 s) |
| `GET /api/repos` | allowlisted local repos (empty until Phase 2) |
| `GET /api/audit?limit=&before_id=&event_type=` | append-only audit trail |
| `GET /api/docs` | OpenAPI UI |

## Decisions worth remembering

1. **`Annotated[AppState, Depends(get_state)]`** instead of `= Depends(...)` defaults — valid FastAPI style and keeps `B008` enabled rather than suppressed.
2. **No ORM.** Hand-written SQL over stdlib `sqlite3`; fewer 3.14 compatibility surfaces.
3. **Migrations are immutable.** `schema_migrations` stores a SHA-256 per file; editing an applied migration raises `MigrationError` instead of silently drifting. Each migration runs in `BEGIN IMMEDIATE` and rolls back fully on failure.
4. **Audit log is append-only in the database**, enforced by `BEFORE UPDATE`/`BEFORE DELETE` triggers, not just by convention. Detail JSON is redacted before insert.
5. **`gh` runs with `GH_TOKEN`/`GITHUB_TOKEN` removed** so it always uses the user's own keyring login; stderr is redacted before it can reach an exception, log, or API body.
6. **Redaction preserves container types.** A logging filter that converted `args` tuples to lists broke `%d` formatting and turned every uvicorn line into a traceback; `redact_value` now keeps tuples as tuples (regression-tested).
7. **Cache lives in `GithubService`,** not in the route, so the scheduler and CLI share the same rate-limit behaviour.
8. **Unexpected exceptions return a generic 500** and log only the redacted message; no stack traces or internals reach the client.

## Verified against the real environment

Live smoke test (throwaway `GITBOT_DATA_DIR`, real `gh` login):

- `/api/health` → `migrations: ["0001_init"]`
- `/api/github/status` → login `a23792809-ops`, scopes `gist, read:org, repo, workflow`
- `/api/github/repos` → 3 repos, all `default_branch: main`
- `/api/audit` → `app.start`, `github.read/auth_status`, `github.read/list_repos`
- Server log: **0** logging errors, **0** credential-shaped strings

## Known limitations (deliberate)

- No write endpoints yet (allowlist add/remove lands in Phase 2).
- No local API token yet: every endpoint is a GET and the socket is loopback-only. Token auth arrives with the first mutating route (Phase 2) so it is never optional.
- `gh repo list` is not used; `gh api user/repos` is, because it returns `default_branch` and `html_url` in one call.
- `starlette` emits a deprecation warning about `httpx` vs `httpx2` in `TestClient`; tests pass, and switching can wait until Starlette requires it.

## Next step

Phase 2: allowlist writes (`POST`/`DELETE /api/repos`) behind the local API token, path-escape checks (realpath + symlink), and dry-run-aware git operations.
