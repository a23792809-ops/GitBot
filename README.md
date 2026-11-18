# GitBot

GitBot is a local-first AI assistant for working on GitHub repositories. It
inspects allowlisted repositories, runs OpenCode on focused coding tasks,
captures the AI response and diff, and asks for explicit approval before
creating a **local** commit.

Everything runs on your machine. GitBot binds to loopback, stores data in
SQLite, keeps the GitHub token in the existing `gh` CLI login, and never pushes,
opens pull requests, merges, or deletes anything.

- **Local-only dashboard** — task console at `http://127.0.0.1:8765/dashboard`.
- **Explicit allowlist** — no repository is touched until you add it.
- **Dry-run by default** — tasks and commits are refused until you opt out.
- **Review before commit** — the exact diff and its SHA-256 hash are stored.
- **No silent writes** — push, pull requests, merges, and repository settings
  changes are not implemented.

## Status

The local core is implemented and tested: repository allowlisting, GitHub
read-only inspection, task creation, execution through OpenCode, cancellation,
diff review, approvals, local commits, audit history, and the dashboard.

The read-only **Analyze** lane and the **code-change** lane both run inside a
real Bubblewrap sandbox with a disposable snapshot. A write task copies `HEAD`
into a private writable `/work/repo`, runs OpenCode there with no original
`.git`, no network, and no host credentials, and generates the review patch
from a host-only baseline after the sandbox exits. The live repository is
untouched until an explicitly approved commit applies the exact reviewed patch.

Both lanes are verified end-to-end with a fake OpenCode binary. Keep
`GITBOT_DRY_RUN=true` unless you are supervising a disposable repository:
dry-run blocks write-task execution and commits.

AI calls use a **host-side gateway** instead of giving the repository sandbox a
network route. The sandboxed OpenCode process talks to an authenticated loopback
port; a tiny relay forwards bytes over one bind-mounted Unix socket to the host
gateway. The gateway validates and bounds the request, then runs a second,
separate OpenCode process in its own Bubblewrap sandbox with no repository and
no host credentials. That host runner is allowed only through an allowlisted
`CONNECT` tunnel to `*.opencode.ai:443`.

The gateway, relay, authentication, request limits, policy verification, and
failure paths are covered by hermetic tests with a fake model runner. A live
provider call is **not** part of the automated suite, so health reports
`provider_verified: false`; run a real task to prove the route on your machine.
See [`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md) for evidence and the
remaining release gates.

Push, pull-request creation, merges, repository deletion, and settings changes
are deliberately **not** implemented. Contribution tracking is read-only, and
the scheduler is opt-in and disabled by default. See
[Known limitations](#known-limitations).

## Requirements

| Tool | Purpose | Tested version |
| --- | --- | --- |
| Python | Runtime | 3.14.4 (requires 3.14+) |
| [uv](https://docs.astral.sh/uv/) | Environment and commands | 0.13.5 |
| Git | Repository operations | 2.53.0 |
| [GitHub CLI](https://cli.github.com/) | Read-only GitHub account/repository data | 2.46.0 |
| [OpenCode](https://opencode.ai/) | Executes coding tasks | v2.0.27 |

`gh` must already be authenticated (`gh auth status`) for the GitHub endpoints.
GitBot reuses that login and never asks for a token of its own.

OpenCode must have a working model route. GitBot's default gateway model is
`opencode/inkling-2-small-free`; the host runner performs model discovery and
inference through the allowlisted `*.opencode.ai` tunnel. The model must be
available to the host `opencode` binary and support the JSON tool protocol.
Presence in a cached model catalog is not proof of availability: GitBot treats
`provider.no-route` and rate-limit responses as explicit failures. The repository
sandbox still has no direct network route. If the host route is unavailable,
tasks fail closed; GitBot never falls back to running the model against the live
repository or to an unsandboxed process.

## Install

```bash
git clone <this-repository> GitBot
cd GitBot
uv sync --extra dev
```

This creates `.venv`, installs runtime and development dependencies, and makes
the `gitbot` command available through `uv run` (or `.venv/bin/gitbot`).

Verify the environment:

```bash
uv run gitbot --check
```

Expected output includes the bind address, dry-run state, applied migrations,
and the database path. `--check` migrates an empty database and exits without
starting the server.

## Configuration

Configuration is layered: defaults, then `.env`, then `GITBOT_*` environment
variables, then CLI overrides. The first `.env` found at `./.env` or
`~/.config/gitbot/.env` is used. Start from the committed example:

```bash
cp .env.example .env
```

| Variable | Default | Notes |
| --- | --- | --- |
| `GITBOT_HOST` | `127.0.0.1` | Loopback only; non-loopback values are rejected. |
| `GITBOT_PORT` | `8765` | 1–65535. |
| `GITBOT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `GITBOT_DRY_RUN` | `true` | Global dry-run; remains `true` by default. |
| `GITBOT_SUPERVISED_AUTOMATION_ENABLED` | `false` | Allows the scheduler to execute sandboxed write lanes to produce diffs/tests/awaiting review while `GITBOT_DRY_RUN=true`. Never auto-approves or auto-commits. |
| `GITBOT_ALLOW_APPROVED_LOCAL_COMMITS` | `false` | Together with supervised automation, allows an explicitly user-approved task to commit locally while `GITBOT_DRY_RUN=true`. No automatic commit/push/PR. |
| `GITBOT_DB_PATH` | `$XDG_DATA_HOME/gitbot/gitbot.db` | SQLite database. |
| `GITBOT_TOKEN_FILE` | `$XDG_CONFIG_HOME/gitbot/token` | Local API bearer token. |
| `GITBOT_DATA_DIR` | `$XDG_DATA_HOME/gitbot` | Data directory. |
| `GITBOT_CONFIG_DIR` | `$XDG_CONFIG_HOME/gitbot` | Configuration directory. |
| `GITBOT_GH_TIMEOUT_S` | `30` | Timeout for `gh` calls. |
| `GITBOT_GITHUB_CACHE_TTL_S` | `60` | GitHub response cache lifetime. |
| `GITBOT_SCHEDULER_ENABLED` | `false` | Start the background scheduler loop. |
| `GITBOT_SCHEDULER_INTERVAL_S` | `30` | Seconds between scheduler ticks. |
| `GITBOT_RETRY_DELAY_S` | `30` | Delay before a created retry task becomes due; `0` retries immediately. |
| `GITBOT_AI_GATEWAY_ENABLED` | `true` | Use the authenticated host gateway. |
| `GITBOT_AI_GATEWAY_MODEL` | `opencode/inkling-2-small-free` | Host runner model; must be available to the host `opencode` binary. |
| `GITBOT_AI_GATEWAY_TIMEOUT_S` | `120` | Host runner timeout, 5–600 seconds. |
| `GITBOT_AI_GATEWAY_RELAY_PORT` | `18080` | Loopback relay port, 1024–65535. |

Defaults for `XDG_DATA_HOME` and `XDG_CONFIG_HOME` are `~/.local/share` and
`~/.config`. Data and config directories are created with `0700` permissions.

### Dry-run and supervised automation

`GITBOT_DRY_RUN=true` is the default and remains the global safety gate:

- Read-only inspection, task creation, and diff review still work.
- Without supervised automation, write tasks are refused/skipped and commits are refused.
- With `GITBOT_SUPERVISED_AUTOMATION_ENABLED=true` (and `GITBOT_DRY_RUN` still `true`), the scheduler may execute **existing sandboxed** write lanes to generate a diff and test results and move the task to `awaiting_review`. The scheduler never auto-approves and never auto-commits.
- Local commit still requires a valid, user-created approval bound to the exact reviewed diff and all existing integrity/test/repo checks. While `GITBOT_DRY_RUN=true`, committing also requires both `GITBOT_SUPERVISED_AUTOMATION_ENABLED=true` and `GITBOT_ALLOW_APPROVED_LOCAL_COMMITS=true` (plus repository `allow_commit` and explicit user action). If either supervised flag is disabled, commit fails closed.
- No automatic GitHub push, pull request, merge, or fake timestamps. Keep all dangerous settings disabled by default and use a disposable profile first.

## Run

```bash
uv run gitbot                 # start the server (default action)
uv run gitbot --serve
uv run gitbot --host 127.0.0.1 --port 9000
uv run gitbot --check
uv run gitbot --rotate-token
uv run gitbot --version
```

The first start creates the API token with `0600` permissions. Rotating it
invalidates the old token immediately. The token is never printed; read it with:

```bash
cat ~/.config/gitbot/token
```

Useful URLs:

- Dashboard: `http://127.0.0.1:8765/dashboard`
- Health: `http://127.0.0.1:8765/api/health`
- OpenAPI docs: `http://127.0.0.1:8765/api/docs`

The server refuses non-loopback binds. Do not expose it through a proxy or
reverse tunnel; there is no multi-user authentication beyond the local bearer
token.

## Dashboard

Open `/dashboard` after starting the server.

1. **Connection** — paste the token from the token file and click **Connect**.
   Repository, task, approval, diff, audit, and GitHub views load only after
   authentication; health stays public. The token is held in memory only and is
   cleared on reload; `GET /api/auth/check` validates it before anything else.
2. **System status** — a live panel above the workspace shows server/dry-run
   state, database path and applied migrations, sandbox capabilities, OpenCode
   and AI toolchain readiness, the scheduler, repository/task counts, and
   pending approvals. “AI toolchain ready” explicitly does **not** claim that a
   model provider has been verified; the panel separately shows whether the host
   gateway is enabled and whether the host runner is installed.
3. **Repositories** — list allowlisted repositories, inspect branch/status,
   toggle **Allow commit**, or remove an entry. Add a repository with an
   absolute path; it must exist, be a git repository, live under your home
   directory, and not be GitBot's own source tree.
4. **Create Task** — choose a repository and type (`analyze`, `explain`,
   `bugfix`, `feature`, `docs`, `deps`, `tests`), describe the work, optionally
   schedule it and set a retry budget, then **Create task** or **Create & run**.
   Analyze and explain are read-only and may run while dry-run is on. Write types
   require supervised automation (or `GITBOT_DRY_RUN=false`) to execute; commits
   still require explicit approval and, in dry-run, both supervised flags.
5. **Tasks** — filter by state, view status, errors, test results, AI response,
   diff, retries, and event history. Use **Run**, **Cancel**, **Retry**,
   **Review**, and **Commit** as the state allows. **Run due now** triggers a
   scheduler tick immediately. Auto-refresh polls while the page is visible.
6. **Task detail** — shows the full prompt, redacted AI output, sandboxed test
   result, stored diff with add/remove/hunk coloring, the diff hash, and the
   review panel. Approval is disabled until you confirm you reviewed the diff;
   the server checks the stored hash and rejects a changed branch, `HEAD`, or
   working tree.
7. **Pending approvals** — only unused, unexpired approvals for tasks still
   awaiting review. They are single-use and bound to a task and diff hash;
   rejecting cancels the task.
8. **Contribution activity** — read-only GitHub summary for the last 30 days,
   including the per-day calendar and commit/issue/PR/review breakdown. GitBot
   never creates artificial activity.

Repositories also have a **Policy** panel for per-repository task-type
enable/disable and an optional test command. The test command is split into a
fixed argv and runs only inside the disposable sandbox; shell operators are
rejected. Failed or errored tests block commit.

The layout is responsive: forms stack, tables scroll horizontally, and the
detail panel uses a fluid metadata grid. All dynamic values are inserted with
`textContent`/DOM nodes, never raw HTML.

## Task workflow

1. **Create** — a task row is stored as `queued` in SQLite.
2. **Preflight** — the repository path is revalidated, and Git must be on a
   branch with a clean working tree and no existing `gitbot/<task>` branch.
3. **Snapshot** — GitBot runs `git archive HEAD` and copies the result into a
   private writable workspace. The source repository and its `.git` directory
   are never mounted into the sandbox.
4. **Policy** — a disposable `opencode.json` in the workspace allows file reads,
   edits, and writes, and denies all shell and execute actions. GitBot verifies
   with `opencode debug config` *inside* the sandbox that the policy loaded,
   then restores any original config before computing the diff.
5. **Run** — OpenCode executes the prompt in `/work/repo` with no network, a
   cleared environment, and no host credentials. Output is redacted and capped
   at 64 KiB. Timeouts, permission denials, process errors, and cancellation are
   classified and recorded.
6. **Review** — a host-only bare git repository, outside the sandbox mount,
   compares the workspace with the baseline. The generated patch (including new,
   deleted, and binary files), its SHA-256 hash, and the full response are
   stored, and an approval is created. Symlink and submodule changes are
   rejected. If the repository has a configured test command, it is split into a
   fixed argv and run inside the same sandbox; its bounded, redacted output and
   exit code are stored. The task becomes `awaiting_review`.
7. **Approve** — the client submits the hash. The server checks the stored diff,
   the repository branch, `HEAD`, and an unchanged working tree. Approving is
   reversible; the approval is not consumed yet.
8. **Commit** — requires `allow_commit` on the repository, non-dry-run, the
   reviewed branch and `HEAD`, an unchanged stored diff, and no failed/errored
   sandboxed test run. The approval is consumed, the exact patch is applied to
   the live index, the resulting diff is hashed again, and a local
   `gitbot: <type> task <id>` commit is created. The task becomes `committed`.

Nothing leaves the machine. There is no push or pull-request step.

**Read-only lane (analyze and explain)** — both use the same snapshot sandbox
but skip review, approval, commit, and tests. The snapshot is mounted read-only
at `/repo`, the policy allows reads only, and OpenCode never sees a writable
workspace. The task stores redacted output and finishes as `analyzed` or
`explained` with no diff, branch, or approval.

**Scheduling and retries** — a task may store a UTC `scheduled_at` timestamp and
a retry budget (`0`–`5`). The scheduler is opt-in (`GITBOT_SCHEDULER_ENABLED`),
process-local, and only runs tasks that are still `queued` when due. It never
runs write tasks while dry-run is enabled; those are skipped and recorded in the
task history. A retry creates a fresh queued task linked to the failed one and
never reuses a partial run.

## Approval and commit safety

- Approvals are single-use, expire after 30 minutes, and are bound to the task,
  repository, action, and diff hash.
- The pending list shows only unused, unexpired approvals whose task is still
  awaiting review; stale rows are never offered for action.
- The dashboard displays the stored diff and hash that approval refers to.
- Approval requires the stored diff hash to match, and the live repository to
  still be on the reviewed branch and `HEAD` with a clean working tree.
- A changed tree or `HEAD` invalidates the approval; re-run or review again.
- A truncated diff is failed at run time and can never be approved or committed.
- Cancelling or rejecting a task revokes its pending approval. Rejecting after a
  task has already left review returns `409` and does not consume the approval.
- Commit re-verifies the stored hash, branch, `HEAD`, dry-run state, repository
  permission, and patch applicability, then consumes the approval exactly once.
- The approval is consumed before `git apply`/`git commit`. If either fails,
  GitBot attempts to reverse its own patch so the clean working tree is restored;
  the approval is still burned and must be recreated by re-running the task. If
  rollback also fails, the error says so and the tree must be inspected.

## Security model

**Path protection**

- Only explicitly allowlisted repositories are used.
- Paths must be absolute, exist, be directories, and contain `.git`.
- Symlinks are resolved before checks; a link cannot escape the allowed root.
- A worktree/submodule `.git` file is accepted only when its `gitdir:` target
  exists, is a directory, and remains inside the allowed roots. A symlinked
  `.git` or a target outside the allowlist is rejected.
- Repositories whose attributes select a Git filter driver are refused, because
  Git would execute repository-controlled filter commands during status, diff,
  or apply. GitBot does not weaken that boundary to support filters.
- Paths must be inside `$HOME` and are rejected if they are GitBot's source tree
  or inside it.
- Stored paths are revalidated on every task run and git API request. If an
  entry disappears or becomes unsafe, the request fails closed and the task
  fails without touching the repository.

**Live Git safety**

- Git commands run with a filtered environment, so `GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_INDEX_FILE`, and similar variables cannot redirect GitBot to another
  repository.
- Command-line configuration disables repository hooks, `core.fsmonitor`, GPG
  signing, pager execution, and background maintenance. Diffs disable external
  diff and textconv commands.
- The reviewed patch is rejected if it writes `.git` metadata or adds,
  modifies, or renames a `.gitattributes` file, so applying it cannot install
  repository-controlled filter execution.
- Commit applies the exact stored patch, re-hashes the applied diff, and rolls
  the patch back if the commit fails.

**Process safety**

- OpenCode is launched with an argument list, never a shell.
- Code-change runs allow file reads, edits, and writes only inside the
  disposable workspace; shell and execute actions are denied. Analyze runs
  allow reads only.
- The policy is verified against the real OpenCode config loader *inside* the
  sandbox before use.
- Any original project config is restored in a `finally` block and never enters
  the review diff.
- Output is redacted for token-shaped values, truncated, and stored locally.
- Running tasks can be cancelled; the process group is terminated and
  escalated to SIGKILL after a grace period.
- Duplicate concurrent runs of the same task are rejected in-process.
- The optional test command is chosen by the user in repository settings, never
  by repository content or the model. It is split with `shlex`, shell operators
  are rejected, and it runs as a fixed argv inside the same sandbox.

**Snapshot sandbox (both lanes)**

`gitbot.security.sandbox` creates a clean `git archive HEAD` snapshot, mounts
it read-only at `/repo`, provides a private writable `/work`, clears the
environment, drops all capabilities, unshares user/PID/network/IPC/UTS/cgroup
namespaces, disables nested user namespaces, and exposes only runtime roots.
`run_sandbox_command` owns a hard timeout, cancellation watchdog, process-group
kill, and bounded output. Tests verify repository writes, `/home`, `/root`,
`/etc`, credentials, and outbound network are denied.

`gitbot.opencode.analysis` mounts the snapshot read-only and allows reads only.
`gitbot.opencode.changes` copies the snapshot into `/work/repo` with no `.git`,
allows file edits there, and keeps the review baseline in a host-only bare git
directory outside the sandbox mount. The original repository's `.git` is never
mounted. If Bubblewrap or policy verification is unavailable, the task fails;
there is no live-repository fallback.

**API and data safety**

- The server binds to loopback only.
- Every endpoint that returns user data requires the bearer token, compared in
  constant time: repository paths, tasks, diffs, approvals, audit rows, git
  status/diff, and GitHub account data. Only `GET /api/health` is unauthenticated.
- The dashboard keeps the token in memory only.
- Secrets are never written to logs, responses, `.env.example`, or the database.
  Logs apply `GITBOT_LOG_LEVEL` and redact every root handler.
- SQLite stores task history, events, approvals, audit records, and repository
  permissions. Migrations are forward-only; never edit an applied migration.
- Removing a repository from the allowlist is refused while task history
  references it. Make it read-only instead.

**Deliberately not implemented**

- Push, pull requests, merges, repository deletion, settings changes.
- Arbitrary shell execution from repository content.
- Automatic commits based on model output.
- Contribution-graph manipulation.
- Unreviewed or failing test runs as a commit gate bypass.

## API

All paths are under `http://127.0.0.1:8765`. Every endpoint that exposes user
data (repository paths, tasks, diffs, approvals, audit, GitHub account data, git
status) requires `Authorization: Bearer <token>`. Only `GET /api/health` is
unauthenticated.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/health` | No | Version, dry-run, migrations, repo count, sandbox/AI/scheduler status. |
| GET | `/api/auth/check` | Yes | Validate the bearer token. |
| GET | `/api/github/status` | Yes | `gh` account and rate limit (read-only). |
| GET | `/api/github/repos` | Yes | Repositories visible to `gh`. |
| GET | `/api/github/contributions` | Yes | Contribution calendar and breakdown (read-only, max 366 days). |
| GET | `/api/repos` | Yes | Allowlisted repositories. |
| POST | `/api/repos` | Yes | Add an allowlisted repository, task policy, and test command. |
| PATCH | `/api/repos/{id}` | Yes | Update commit permission, allowed task types, or test command. |
| DELETE | `/api/repos/{id}` | Yes | Remove an entry; refused if it has task history. |
| GET | `/api/repos/{id}/git/status` | Yes | Branch, upstream, ahead/behind, changed files. |
| GET | `/api/repos/{id}/git/diff` | Yes | Read-only working-tree diff. |
| GET | `/api/repos/{id}/git/preflight` | Yes | Report whether a task branch could be created. |
| GET | `/api/tasks` | Yes | Task list (large output/patch omitted). |
| POST | `/api/tasks` | Yes | Create a task, optionally with `scheduled_at` and `max_retries`. |
| GET | `/api/tasks/{id}` | Yes | Full task, stored diff, and event history. |
| POST | `/api/tasks/{id}/run` | Yes | Run a queued task (blocked by dry-run). |
| POST | `/api/tasks/{id}/cancel` | Yes | Cancel queued, running, or reviewing tasks. |
| POST | `/api/tasks/{id}/retry` | Yes | Create a queued retry from a failed task within its budget. |
| POST | `/api/tasks/{id}/commit` | Yes | Local commit after approval and passing tests. |
| GET | `/api/tasks/scheduler/status` | Yes | Scheduler enabled state, interval, batch size, next due task. |
| POST | `/api/tasks/scheduler/tick` | Yes | Run due queued tasks now. |
| GET | `/api/tasks/approvals/pending` | Yes | Unused approvals. |
| POST | `/api/tasks/approvals/{id}/approve` | Yes | Approve an exact diff hash. |
| POST | `/api/tasks/approvals/{id}/reject` | Yes | Reject and cancel the task. |
| GET | `/api/audit` | Yes | Audit trail with filters and pagination. |

Example:

```bash
TOKEN=$(cat ~/.config/gitbot/token)
curl -s http://127.0.0.1:8765/api/repos
curl -s -X POST http://127.0.0.1:8765/api/repos \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"/home/you/projects/example","allow_commit":false}'
```

## Data and migrations

The SQLite database lives at `~/.local/share/gitbot/gitbot.db` unless
`GITBOT_DB_PATH` or `XDG_DATA_HOME` says otherwise. Schema files are in
`backend/gitbot/schema/` and are applied in filename order:

- `0001_init.sql` — repositories, audit log.
- `0002_tasks.sql` — tasks, events, approvals.
- `0003_task_output.sql` — stored AI output.
- `0004_task_review.sql` — stored diff, hash, truncation flags.
- `0005_analysis_state.sql` — terminal `analyzed` state, sandbox metadata.
- `0006_explain_scheduler_tests.sql` — `explained` state/type, per-repo task
  policy and test command, scheduling/retry fields, sandboxed test results.

Migrations `0005` and `0006` were written and tested on fresh and upgraded
databases, then applied to the existing user profile on 2026-11-15 after a
backup was taken. Migration `0007_daily_schedules.sql` was applied in the same
pass. Migrations are forward-only. Back up the database before upgrading,
never edit an applied migration, and add a new numbered file for changes.
`gitbot --check` applies pending migrations without starting the server.

Migrations are forward-only. Back up the database before upgrading, never edit
an applied migration, and add a new numbered file for changes. `gitbot --check`
applies pending migrations without starting the server.

## Documentation

- [`docs/TESTING.md`](docs/TESTING.md) — disposable profile and the safe local testing workflow.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — layers, task lifecycle, sandbox, scheduler.
- [`docs/API.md`](docs/API.md) — endpoints, payloads, errors, examples.
- [`docs/SECURITY.md`](docs/SECURITY.md) — trust boundaries, sandbox, approvals, tests.
- [`docs/BACKUP.md`](docs/BACKUP.md) — backup, migration, and restore procedure.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common failures and fixes.
- [`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md) — audit, evidence, blockers.

## Development

```bash
uv run pytest                     # full suite
uv run pytest -m "not slow"       # skip real-machine/Binary tests
uv run ruff check .               # lint
uv run ruff format --check .      # formatting
uv run python -m compileall backend tests
uv run gitbot --check             # config + migrations
```

Tests use real FastAPI routing, real SQLite, real Git, and a fake `opencode`
executable unless marked `slow`. Integration tests for the engine live in
`tests/integration/`. Never use the GitBot source tree as a test repository;
create a separate throwaway repository for manual end-to-end checks.

Project layout:

```text
backend/gitbot/
  api/          FastAPI app, routes, schemas, auth deps, state
  gitops/       read-only Git inspection
  github/       gh CLI client and read-only GitHub service
  opencode/     adapter, parser, policy generation/verification
  security/     allowlist, token handling, redaction
  tasks/        engine, approvals, state
  schema/       forward-only SQL migrations
  static/       dashboard (single-file HTML/CSS/JS)
tests/          unit and integration tests
docs/           design notes and phase plans
```

## Troubleshooting

**`401 unauthorized`** — send `Authorization: Bearer <token>`; confirm the
server's `GITBOT_TOKEN_FILE` matches the file you read. If the token was
rotated, reconnect the dashboard.

**`403 dry-run`** — write tasks and commits are disabled. Analyze tasks are
read-only and still allowed. To run write tasks, set `GITBOT_DRY_RUN=false`,
restart, and supervise the task.

**`OpenCode executable not found`** — install OpenCode and ensure `opencode`
is on `PATH` for the server process. Check with `opencode --version`.

**`OpenCode policy verification failed`** — the installed OpenCode version did
not load the generated policy. Inspect the error, verify `opencode debug config`
works in the repository, and do not bypass the check.

**`preflight failed`** — the repository is detached, has uncommitted changes, or
already has the `gitbot/<task>` branch. Commit/stash your work or choose another
repository.

**`approval_mismatch`** — the working tree, branch, or `HEAD` changed since
review. Inspect the repository, then re-run the task to produce a fresh diff and
approval.

**Repository rejected** — the path must be an absolute git repository inside
your home directory. GitBot's own source tree is always refused, as are
symlinks that escape the allowed root.

**Dashboard unreachable** — confirm the server is running on the printed
loopback address/port and that no proxy is intercepting localhost.

## Known limitations

- **The AI gateway is tested hermetically, not against a live provider.** Both
  lanes and the gateway/relay path are verified with fake runners. Health
  deliberately reports `provider_verified: false`; run a real task to prove the
  host route in your environment.
- **The host gateway is the only AI route.** The repository sandbox has no
  direct network. The host runner is restricted to `*.opencode.ai:443`; an
  offline/local model would need a reviewed gateway provider and runner change.
  There is no unsandboxed fallback.
- Sandboxed tests run only when a repository has an explicit `test_command`, and
  only as a fixed argv with no shell. They run in the disposable workspace and
  never on the live repository. A failed or errored test run blocks commit.
- Commit applies the reviewed patch to the current branch; it does not create a
  task branch, push, open pull requests, merge, or perform GitHub writes.
- Contribution tracking is read-only. It shows the calendar and breakdown from
  GitHub; it does not diagnose attribution beyond GitHub's author-email and
  default-branch rules, and it never fabricates activity.
- The scheduler is opt-in (`GITBOT_SCHEDULER_ENABLED=false` by default),
  process-local, and interval-based; there is no cron parser. While dry-run is
  enabled it skips write tasks and records the skip. Tasks still run
  synchronously from the API when triggered manually.
- A retry creates one fresh queued child within a per-task budget (`0`–`5`); it
  is not an automatic in-process re-run. Retry the newest failed task to use a
  larger budget. A failed task stays `failed` until a retry is created and run.
- Reminders and notifications are not implemented. The scheduler runs
  already-created tasks; it does not send alerts or reminders.
- The dashboard is static HTML/CSS/JS served by FastAPI; the `frontend/`
  Next.js scaffold is not part of the running application.
- Cancellation of a task running in a different process is refused (the cancel
  registry is per-process). If the server crashes, the next startup marks the
  stale `running` task as `failed` and tells you to inspect the working tree.
- If `git apply` or `git commit` fails after approval consumption, GitBot
  attempts to reverse its own patch. The approval is burned either way, and a
  failed rollback is reported for manual inspection.
- GitBot refuses repositories that use Git filter drivers (for example a
  repository-level LFS filter). This is a deliberate security restriction, not
  a supported fallback.
- Legacy `awaiting_review` rows created before the sandboxed write lane have no
  stored sandbox diff; they can be inspected or rejected but must be re-run
  before approval or commit.
- The bearer token is a local single-user secret, not a multi-user auth system.
- The dashboard is static HTML/CSS/JS served by FastAPI; `frontend/` is not part
  of the running application.
- GitHub reads require the external `gh` CLI and its existing login.
