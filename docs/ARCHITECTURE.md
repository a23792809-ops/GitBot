# Architecture

GitBot is a single-process, local-first service. A FastAPI app owns the API and
serves the static dashboard; a task engine runs OpenCode inside a Bubblewrap
sandbox; SQLite stores repositories, tasks, approvals, events, and audit records.
There is no frontend build step and no remote service.

## Layers

```text
CLI (`gitbot`) ──► config / startup checks
                      │
FastAPI app ──► API routes ──► services ──► SQLite, Git, `gh`, OpenCode
      │                              │
      └── static dashboard           └── Bubblewrap sandbox
```

| Package | Responsibility |
| --- | --- |
| `gitbot/config.py` | Layered settings (defaults, `.env`, `GITBOT_*`, CLI), loopback and dry-run validation. |
| `gitbot/db.py` | SQLite connection helpers, forward-only migration runner, startup recovery hooks. |
| `gitbot/repos.py` | Allowlisted repository store and task-policy/test-command normalization. |
| `gitbot/security/` | Path allowlist, bearer-token handling, redaction, Bubblewrap runner. |
| `gitbot/gitops/` | Read-only Git inspection and preflight checks. |
| `gitbot/github/` | `gh` CLI client and read-only GitHub service (account, repos, contributions). |
| `gitbot/opencode/` | OpenCode adapter, output parser, policy generation, host AI gateway, analysis/change lanes. |
| `gitbot/tasks/` | Task engine, state machine, approvals, scheduler. |
| `gitbot/api/` | Routes, schemas, dependencies, app wiring, static dashboard. |
| `gitbot/schema/` | Numbered forward-only SQL migrations. |

## Task lifecycle

```text
queued ──► running ──► analyzed / explained        (read-only lanes)
                  ├──► awaiting_review ──► approved ──► committed
                  ├──► failed
                  └──► cancelled
queued ──► cancelled / expired
```

- **queued** — stored by `POST /api/tasks`. May carry `scheduled_at`, `max_retries`,
  and a `parent_task_id` for retries.
- **running** — the engine registers an in-process cancel handle plus a
  database-driven watchdog (`_watch_cancel_requests`), revalidates the
  repository, and starts the sandbox. Any process sharing the same SQLite
  database can request cancellation by writing a `task.cancel_requested`
  event; the watchdog observes it within ~1 s and raises the in-memory signal.
- **analyzed / explained** — read-only lanes. No diff, branch, approval, or commit.
- **awaiting_review** — a change task produced a patch, SHA-256 hash, approval,
  and optional sandboxed test result.
- **approved** — a user approved the exact stored hash; approving is reversible.
- **committed** — the exact patch was applied locally and committed. Nothing is
  pushed.
- **failed / cancelled / expired** — terminal states; a retry creates a new queued
  task rather than mutating the failed one.

The state machine lives in `gitbot/tasks/state.py` and rejects invalid
transitions. The engine updates state and writes a `task_events` row together.

## Sandboxed execution

Both lanes use `gitbot.security.sandbox.run_sandbox_command`:

1. The engine revalidates the allowlisted path and Git preflight (branch, clean
   tree, no existing task branch).
2. `git archive HEAD` creates a disposable snapshot. The live `.git` is never
   mounted into the sandbox.
3. Bubblewrap unshares user/PID/network/IPC/UTS/cgroup namespaces, drops
   capabilities, clears the environment, disables nested user namespaces, and
   exposes only runtime roots.
4. **Analyze/explain:** the snapshot is mounted read-only at `/repo`; the policy
   allows reads only.
5. **Change:** the snapshot is copied to a writable `/work/repo` with no `.git`;
   the policy allows reads/edits/writes there and denies shell/execute. A
   host-only bare Git repository outside the sandbox records the baseline.
6. The engine verifies the generated OpenCode policy with
   `opencode debug config` *inside* the sandbox before running the task.
7. Output is redacted and capped. Run results are classified as success, timeout,
   cancellation, permission denied, or process/protocol error.
8. For change tasks, the diff is computed before optional tests, then the
   configured test command runs in the same sandbox. Failed/errored tests block
   commit.

There is no fallback to running against the live repository if Bubblewrap or
policy verification is unavailable. The task fails closed.

## Host AI gateway

The repository sandbox never receives provider credentials or a direct network
route. When `GITBOT_AI_GATEWAY_ENABLED=true` (the default), each task opens a
short-lived gateway:

1. `gitbot.opencode.gateway.AIGateway` starts an HTTP server on a private Unix
   socket with a per-run capability token (`secrets.token_urlsafe`).
2. `gitbot.security.sandbox_relay` is mounted read-only into the repository
   sandbox. It listens on `127.0.0.1:<relay_port>` and relays bytes to the
   bind-mounted gateway socket. The relay parses no HTTP and executes a fixed
   argv chosen by GitBot.
3. The sandboxed OpenCode policy is generated with an OpenAI-compatible provider
   pointing at `http://127.0.0.1:<relay_port>/v1`. The policy is verified inside
   the sandbox, including the gateway provider and model.
4. The gateway authenticates every request with the capability token, normalizes
   messages/tools, applies size/count limits, and converts the request into an
   untrusted-data prompt.
5. `HostModelRunner` runs a second OpenCode process in its own Bubblewrap
   sandbox: a private temporary tree, no repository, no host home/credentials,
   and only an allowlisted HTTP `CONNECT` tunnel to `*.opencode.ai:443`.
6. The model must return one JSON object (`final` or `tool`). The gateway
   validates the tool name against the request's tool list, retries one invalid
   JSON answer, bounds output, and redacts provider errors before returning an
   OpenAI-compatible response (JSON or SSE) to the sandbox.

If the gateway or host runner is unavailable the task fails; there is no
unsandboxed fallback and no direct-network fallback. The gateway is tested with
fake runners, so health reports `provider_verified: false`.

## Scheduler and retries

`gitbot/tasks/scheduler.py` is opt-in (`GITBOT_SCHEDULER_ENABLED=false`). It is a
daemon thread that ticks every `GITBOT_SCHEDULER_INTERVAL_S` seconds. A
non-blocking lock ensures only one tick runs at a time. A tick selects at most
`batch_size` queued tasks whose `scheduled_at` is due, runs them one at a time,
and records `task.scheduler.*` events. Write tasks are skipped while dry-run is
enabled. `POST /api/tasks/scheduler/tick` runs the same logic manually.

Retries are explicit: `POST /api/tasks/{id}/retry` creates a fresh queued task
from a `failed` task while `retry_count < max_retries` (0–5) and links it with
`parent_task_id`. Nothing reuses a partial workspace or a failed approval.

There are no reminders or notifications. The scheduler only decides when an
already-created, validated task may run.

## HTTP surface

FastAPI routes live under `backend/gitbot/api/routes/`. `GET /api/health` is the
only unauthenticated read; every other endpoint that returns user data requires
`Authorization: Bearer <token>` via the `require_token` dependency. That includes
repositories and their paths, tasks (prompts, output, stored diffs), approvals,
audit rows, git status/diff/preflight, scheduler status, and GitHub account data.
The bind address is loopback-only. The dashboard sends the token for those reads
and treats a 401 as "connect a token" rather than an empty data set.

## Data model

- `repos` — allowlisted path/slug, branch, `allow_commit`, `allow_push`,
  `allowed_task_types`, `test_command`.
- `tasks` — prompt, type, state, timestamps, output/diff, sandbox metadata,
  approval ID, scheduling/retry fields, test results.
- `task_events` — append-only task history with actor and JSON detail.
- `approvals` — single-use, expiring, diff-hash-bound records.
- `audit_log` — security-relevant actions and outcomes.

Migrations are forward-only files in `backend/gitbot/schema/` and are applied in
filename order. Never edit an applied migration.

## Dashboard

`backend/gitbot/static/dashboard/index.html` is a single static file served by
FastAPI. It has no build step and no framework. All dynamic values are inserted
with `textContent` or DOM builders; there is no `innerHTML` sink. The token is
held in memory only and is sent as a bearer header for repository, task,
approval, audit, git, and GitHub reads; health stays public. The Node VM harness in `tests/frontend/` executes the real
inline script against a small DOM/fetch shim.

## Extension points

- Add a task type: extend `TaskType`, `ALL_TASK_TYPES`, `READ_ONLY_TASK_TYPES`,
  the migration CHECK constraint, the dashboard select, and tests.
- Add a guarded action: create an approval action, enforce it in the route/engine,
  and keep it single-use and diff-bound.
- Add a migration: create the next numbered SQL file and update migration tests.
