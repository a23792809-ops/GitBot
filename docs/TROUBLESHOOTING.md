# Troubleshooting

## Server and configuration

**Server refuses to start / non-loopback bind** — `GITBOT_HOST` must be a loopback
address. Do not expose the API through a proxy or tunnel.

**Port already in use** — choose another loopback port:
`uv run gitbot --port 9000`.

**401 unauthorized** — send `Authorization: Bearer <token>`. Confirm
`GITBOT_TOKEN_FILE` matches the file you read. If the token was rotated,
reconnect the dashboard. The token is never printed by the CLI.

**Dashboard shows "Unreachable"** — confirm the server is running on the printed
address/port and no proxy intercepts localhost.

**Migrations pending / schema errors** — back up first, review the migration, then
run `uv run gitbot --check` against a throwaway profile. See
[`BACKUP.md`](BACKUP.md) and [`TESTING.md`](TESTING.md). The real profile has
`0001`–`0007` applied; any *new* migration requires a backup and explicit
approval before it touches the real profile. Never edit an applied migration or
delete the database.

## Repositories

**Repository rejected** — the path must be absolute, exist, be a Git repository
inside `$HOME`, and not be GitBot's own source tree. Symlinks that escape the
allowed root are refused.

**`preflight failed`** — the repository is detached, has uncommitted changes, or
already has the `gitbot/<task>` branch. Commit/stash your work or choose another
repository.

**Cannot remove a repository** — it has task history. Disable commit and task
types instead; removal is refused while history references it.

**Task type refused** — the repository's `allowed_task_types` does not include it.
Open the dashboard **Policy** panel or `PATCH /api/repos/{id}`.

## Tasks and AI

**`403 dry-run`** — write tasks and commits are disabled. Analyze/explain are
read-only and still allowed. To run write tasks, set `GITBOT_DRY_RUN=false`,
restart, and supervise a disposable repository.

**`OpenCode executable not found`** — install OpenCode and ensure `opencode` is on
`PATH` for the server process. Check with `opencode --version`.

**`OpenCode policy verification failed`** — the installed OpenCode version did not
load the generated policy. Verify `opencode debug config` works, inspect the
error, and do not bypass the check.

**AI task fails with a network/model error** — the repository sandbox has no
direct network by design. Check `GET /api/health` → `ai.gateway`: it should show
`enabled: true`, `host_runner_available: true`, and a model name. Confirm the host
`opencode` binary can run the configured model. Do not grant the repository
sandbox network access or copy credentials into `/work`.

**`ai_gateway_error` / host runner exited** — the second, host-side OpenCode
sandbox failed or timed out. Check the host model name, quota, and
`GITBOT_AI_GATEWAY_TIMEOUT_S`. Provider error text is redacted before it reaches
the task; inspect server logs only if needed and never paste tokens.

**`ai_gateway_bad_request`** — the sandboxed client sent a malformed, oversized,
or unsupported OpenAI request. This usually means the installed OpenCode version
changed its request shape; verify `opencode debug config` and the gateway limits
before changing policy.

**Gateway port already in use** — set `GITBOT_AI_GATEWAY_RELAY_PORT` to another
loopback port and restart. The relay only binds `127.0.0.1`.

**Task stuck `queued`** — run it manually, or schedule it and enable the
scheduler. The scheduler only picks due `queued` tasks.

**Scheduled task did not run** — the scheduler is off by default. Enable
`GITBOT_SCHEDULER_ENABLED=true`, check `GET /api/tasks/scheduler/status`, or use
**Run due now** / `POST /api/tasks/scheduler/tick`. Write tasks are skipped while
dry-run is on; inspect the task history for `task.scheduler.skipped`.

**Retry disabled** — the task is not `failed`, or its retry budget is exhausted
(`retry_count >= max_retries`). Create a new task instead.

**Task timed out / cancelled** — check the task event history. A task that is
running in this process cancels immediately; a task owned by another GitBot
process (e.g. a second server sharing the same SQLite database) is stopped by a
durable `task.cancel_requested` event that the owning process observes within
about a second via its cancel watchdog, then SIGTERMs the sandbox. On startup,
stale `running` rows are marked `failed`; inspect the working tree before
retrying.

## Review, tests, and commit

**`approval_mismatch`** — the working tree, branch, or `HEAD` changed since
review, or the submitted hash does not match. Inspect the repository and re-run
the task to produce a fresh diff and approval.

**Approval expired** — approvals last 30 minutes. Re-run or approve again before
expiry.

**Tests failed and commit is blocked** — the configured sandboxed test command
returned non-zero or errored. Inspect the stored test output and fix the change.
Do not remove the test command merely to bypass the gate.

**Test command rejected** — it must be a single line, at most 500 characters, and
must not contain shell operators. Use an argv-style command such as
`uv run pytest -q`, not `pytest && echo ok`.

**`there is nothing to commit`** — the reviewed patch is empty.

**Commit failed after approval** — the approval was consumed before `git apply`/
`git commit`; if Git failed, it is burned. Re-run the task to create a new
approval.

## Contributions

**Contribution request fails** — GitBot uses the existing `gh` login. Check
`gh auth status`. The endpoint is read-only and bounded to 366 days.

**No contribution squares / missing activity** — GitHub attributes contributions
to the commit author email and the repository's default branch. GitBot does not
change commit dates or fabricate activity. Diagnose attribution in GitHub itself.

## Sandbox

**Bubblewrap unavailable** — tasks fail closed. Install Bubblewrap only with
approval, and do not add a live-repository fallback.

**Sandbox test failures on the host** — some environments disable unprivileged
user namespaces. Verify with `bwrap --unshare-user --disable-userns true` only in
a disposable test context; do not weaken production isolation to make a test
pass.

## Tests

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q backend tests
git diff --check
node --test tests/frontend/dashboard_form.test.mjs
```

If the Node dashboard test crashes with a missing element, the script references
an ID that is absent from `FORM_IDS` in `tests/frontend/dashboard_form.test.mjs`.
Add the element to both the dashboard and the harness.
