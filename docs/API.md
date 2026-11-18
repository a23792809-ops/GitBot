# Local API

Base URL: `http://127.0.0.1:8765` (loopback only). Interactive schema:
`/api/docs`.

Every endpoint that returns user data requires:

```http
Authorization: Bearer <token>
```

That includes repository paths, task prompts/outputs/diffs, approvals, the audit
log, GitHub account data, and git status/diff. Only `GET /api/health` and the
static dashboard are unauthenticated readiness/diagnostic surfaces. Mutating
endpoints always require the token as well.

The token is created by `gitbot --rotate-token` and stored with `0600`
permissions. The server compares it in constant time. Never put it in a URL,
commit, or log.

## Health and auth

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/health` | No | Version, dry-run, DB path/migrations, repo count, sandbox capabilities, OpenCode probe, AI readiness, scheduler status. |
| GET | `/api/auth/check` | Yes | Validates the token; the dashboard uses it before enabling writes. |

Example health fields:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "dry_run": true,
  "local_only": true,
  "database": {"path": "...", "migrations": ["0001_init", "..."]},
  "allowlisted_repos": 1,
  "sandbox": {"bwrap_path": "...", "version": "...", "can_run": true, "...": "..."},
  "opencode": {"available": true, "...": "..."},
  "ai": {
    "ready": true,
    "toolchain_ready": true,
    "provider_verified": false,
    "sandbox_network_isolated": true,
    "gateway": {
      "enabled": true,
      "model": "opencode/inkling-2-small-free",
      "host_runner_available": true,
      "transport": "loopback-relay-to-authenticated-unix-socket",
      "note": "..."
    },
    "note": "..."
  },
  "scheduler": {"enabled": false, "interval_seconds": 30, "next_scheduled_at": null}
}
```

## GitHub (read-only)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/github/status` | Yes | `gh` account, scopes, rate limit. |
| GET | `/api/github/repos?limit=100&refresh=false` | Yes | Repositories visible to `gh`. |
| GET | `/api/github/contributions?from=YYYY-MM-DD&to=YYYY-MM-DD` | Yes | Contribution calendar and breakdown. Defaults to the last 30 days; max 366 days. |

Contributions are read-only. GitBot does not create commits, reviews, or issues
to influence the graph.

## Repositories

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/repos` | Yes | Allowlisted repositories. |
| POST | `/api/repos` | Yes | Add a repository. |
| PATCH | `/api/repos/{id}` | Yes | Update commit permission, allowed task types, or test command. |
| DELETE | `/api/repos/{id}` | Yes | Remove an entry; refused while task history references it. |
| GET | `/api/repos/{id}/git/status` | Yes | Branch, upstream, ahead/behind, changed files. |
| GET | `/api/repos/{id}/git/diff` | Yes | Read-only working-tree diff; optional `base` revision and `staged` flag. |
| GET | `/api/repos/{id}/git/preflight` | Yes | Whether a task branch could be created. |

Create:

```bash
curl -s -X POST http://127.0.0.1:8765/api/repos \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "/home/you/projects/example",
    "slug": "you/example",
    "default_branch": "main",
    "allow_commit": false,
    "allowed_task_types": ["analyze", "explain", "docs"],
    "test_command": "uv run pytest -q"
  }'
```

`path` must be absolute, exist, be a Git repository, live under `$HOME`, and not
be GitBot's source tree. `allow_push` cannot be enabled. An empty
`allowed_task_types` list is rejected. `test_command` must be a single line,
at most 500 characters; it is split with `shlex` and run without a shell.

Update:

```bash
curl -s -X PATCH http://127.0.0.1:8765/api/repos/1 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"allowed_task_types": ["analyze", "docs"], "test_command": null}'
```

Sending `"test_command": null` clears it. Omitting the field leaves it unchanged.

## Tasks

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/tasks` | Yes | Latest 100 tasks; prompt truncated, output/patch omitted. |
| POST | `/api/tasks` | Yes | Create a task. |
| GET | `/api/tasks/{id}` | Yes | Full task, stored diff, and event history. |
| POST | `/api/tasks/{id}/run` | Yes | Run a queued task (write tasks blocked by dry-run). |
| POST | `/api/tasks/{id}/cancel` | Yes | Cancel queued, running, reviewing, or approved task. |
| POST | `/api/tasks/{id}/retry` | Yes | Create one queued retry from a failed task within budget. |
| POST | `/api/tasks/{id}/commit` | Yes | Local commit after approval and passing tests. |

Task types: `analyze`, `explain`, `bugfix`, `feature`, `docs`, `deps`, `tests`.

Create:

```json
{
  "repo_id": 1,
  "task_type": "docs",
  "prompt": "Document the configuration parser.",
  "scheduled_at": "2026-11-05T09:00:00Z",
  "max_retries": 2
}
```

- `scheduled_at` is optional ISO-8601 with a timezone; it is normalized to UTC.
- `max_retries` is optional, integer 0–5.
- `prompt` is required, at most 32,000 characters.
- A disabled task type for the repository is refused at creation and is checked
  again immediately before execution.
- A failed task can produce at most one retry child; retry the newest failed
  task when a budget remains.

Task states: `queued`, `running`, `awaiting_review`, `analyzed`, `explained`,
`approved`, `committed`, `failed`, `cancelled`, `expired`.

Detail response:

```json
{
  "task": {
    "id": "tsk_...",
    "repo_id": 1,
    "task_type": "docs",
    "state": "awaiting_review",
    "diff_hash": "...",
    "tests_status": "passed",
    "test_exit_code": 0,
    "retry_count": 0,
    "max_retries": 2
  },
  "events": [{"event_type": "task.created", "actor": "system", "detail": "{}"}],
  "diff": {"patch": "...", "files": ["README.md"], "truncated": false, "stored": true}
}
```

`commit` refuses when dry-run is enabled, `allow_commit` is false, no sandboxed
review diff exists, the stored hash changed, the branch/HEAD/working tree
changed, the diff is truncated, or a sandboxed test run failed/errored.

## Approvals

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/tasks/approvals/pending` | Yes | Actionable approvals: unused, unexpired, and still awaiting review. |
| POST | `/api/tasks/approvals/{id}/approve` | Yes | Approve an exact diff hash. |
| POST | `/api/tasks/approvals/{id}/reject` | Yes | Reject and cancel the task. |

```json
{"diff_hash": "<sha256 from the task detail>"}
```

Approvals are single-use, expire after 30 minutes, and are bound to the task,
repository, action, and diff hash. The pending endpoint excludes expired rows
and tasks that are no longer awaiting review. Approval is refused if the
repository is no longer allowlisted, the working tree is dirty, or the
branch/HEAD/hash changed. The approval is consumed only after the exact reviewed
patch is staged for the local commit. A crash during commit is journaled and
startup recovery fails the task and revokes the approval, so a stale approval can
never be replayed. Rejecting a task that already left review returns `409` and does
not consume the approval.

## Scheduler

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/tasks/scheduler/status` | Yes | Enabled, interval, batch size, next scheduled time. |
| POST | `/api/tasks/scheduler/tick` | Yes | Run due queued tasks now, regardless of the background toggle. |

The tick returns `{"scheduler": {"results": [...], "count": N}}`. A concurrent
tick returns `{"skipped": "a scheduler tick is already running", "results": []}`.
Write tasks are skipped (with a task event) while dry-run is enabled.

## Errors

Errors use either a string `detail` or a structured object:

```json
{"detail": {"error": {"code": "approval_mismatch", "message": "..."}}}
```

Common codes: `unauthorized`, `missing_token`, `validation_error`, `dry_run`,
`approval_invalid`, `approval_expired`, `approval_mismatch`,
`invalid_state_transition`, `not_found`.

## Audit

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/audit` | Yes | Audit trail with filters and pagination. |

## Example flow

```bash
TOKEN=$(cat ~/.config/gitbot/token)
BASE=http://127.0.0.1:8765

curl -s "$BASE/api/repos" -H "Authorization: Bearer $TOKEN"
TASK=$(curl -s -X POST "$BASE/api/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":1,"task_type":"analyze","prompt":"Summarize the entry points."}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["id"])')
curl -s -X POST "$BASE/api/tasks/$TASK/run" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/api/tasks/$TASK" -H "Authorization: Bearer $TOKEN"
```

Analyze/explain are read-only and allowed while dry-run is enabled. Write tasks
and commits require `GITBOT_DRY_RUN=false` and explicit supervision.
