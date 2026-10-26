# Phase 2a — Allowlist writes and local API authentication

Status: **complete**. 130 tests passing, lint and format clean.

## What changed

| Area | Addition |
|---|---|
| `security/allowlist.py` | `resolve_repo_path()` — absolute, exists, is a directory, is a git repo, inside allowed roots, never inside GitBot's own tree |
| `security/tokens.py` | `ensure_token`, `read_token`, `rotate_token`, `tokens_match` (constant-time) |
| `api/deps.py` | `require_token` bearer gate for every mutating route |
| `api/schemas.py` | `RepoCreate` / `RepoUpdate` with `extra="forbid"` and branch-name validation |
| `repos.py` | `add`, `get`, `find_by_path`, `update`, `delete` with 409 on duplicate path |
| `api/routes/repos.py` | `POST /api/repos`, `PATCH /api/repos/{id}`, `DELETE /api/repos/{id}` |
| `main.py` | creates the token on serve; `gitbot --rotate-token` to rotate |

## Authentication flow

1. `gitbot` (serve) creates `~/.config/gitbot/token` if missing — directory `0700`, file `0600`, created with `O_EXCL` so a secret is never briefly world-readable.
2. Mutating requests send `Authorization: Bearer <token>`.
3. The dependency re-reads the file per request (so rotation takes effect without restart), refuses files with group/other permission bits, and compares with `hmac.compare_digest`.
4. The token value is never returned by an endpoint, never written to the audit log, and `--rotate-token` prints only the path.

Reads stay unauthenticated because they are GET-only and the socket is loopback-only; every route that can change state requires the token.

## Policy enforcement, in order

1. Path is non-empty and absolute (`~/...` is expanded).
2. Path exists and resolves (`resolve(strict=True)`).
3. Path is a directory.
4. Path contains `.git` (directory or worktree file).
5. Path is not GitBot's own source tree, or anything inside it.
6. Resolved path is inside an allowed root (default: `$HOME`).

Because the containment check runs on the **resolved** path, a symlink such as `~/escape -> /etc` is rejected. Denials are audited with `outcome=denied` and the stable error code, then surfaced as HTTP 403 `path_not_allowed`.

## Verified

- `gitbot --rotate-token` creates a `0600` file, prints only the path, and a second rotation invalidates the first token (unit-tested; CLI exercised against a throwaway `GITBOT_TOKEN_FILE`).
- `POST /api/repos` without/with wrong token → 401; with token → 201 and an audit row; duplicate → 409; non-git or relative path → 403 `path_not_allowed` plus a `denied` audit row; unsafe branch name or unknown JSON field → 422.
- `PATCH` toggles `allow_commit`; `DELETE` returns 204 and removes the row; both audited.

## Deliberate limitations

- **No git operations yet.** `allow_commit`/`allow_push` are stored capabilities, not yet consumed by any executor. Nothing in this phase can modify a working tree or a remote.
- Slug defaults to `local/<dirname>` when not supplied; deriving it from `git remote get-url origin` belongs with the git adapter.
- No rate limiting on the local API; loopback + token is considered sufficient for a single-user local tool. Revisit if the dashboard is ever exposed beyond localhost (it will not be, by policy).

## Phase 2b — read-only git inspection

`backend/gitbot/gitops/repo.py` adds:

| Method | Git command | Notes |
|---|---|---|
| `status()` | `status --porcelain=v2 --branch -z` | branch, HEAD, upstream, ahead/behind, changed files, detached detection |
| `diff(staged=, base=, max_chars=)` | `diff --no-color --no-ext-diff` + `diff --name-only` | patch capped at 256 KiB by default; `truncated` flag |
| `branch_exists(name)` | `branch --list --format=...` | used by preflight |
| `preflight(task_id)` | reads only | reports dirty tree, detached HEAD, existing `gitbot/<id>` branch |

Process hygiene: fixed `cwd`, argv lists, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=true`, `GIT_PAGER=cat`, `LC_ALL=C` for stable parsing, redacted stderr in errors.

HTTP (GET only, no token needed, loopback only):

```
GET /api/repos/{id}/git/status
GET /api/repos/{id}/git/diff?staged=false&base=HEAD~1
GET /api/repos/{id}/git/preflight?task_id=run-42
```

The adapter exposes **no** checkout, branch creation, commit, or push. A test asserts that calling all three endpoints leaves `HEAD` and the working tree byte-identical.

## Next step

Phase 3: task engine + OpenCode Tier 1 (`opencode run --format json`), on a `gitbot/<task-id>` branch created only after the preflight passes and the task is approved.

