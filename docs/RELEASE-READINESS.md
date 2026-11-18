# Release Readiness Audit

**Date:** 2026-11-03 (updated 2026-11-18)
**Readiness:** **2. SUPERVISED AI TEST READY**
**Verdict:** **offline MVP and the supervised-test harness are ready; a live
provider call is still outstanding.** The real-profile migrations `0005`–`0007`
were applied on 2026-11-15 after a backup. The local core, sandboxed lanes,
host AI gateway, approvals, scheduler, dashboard,
and migrations are implemented and covered by the automated suite. That suite
uses fake model runners, so this is not **3. REAL AI VERIFIED** and definitely
not **4. PRODUCTION READY**.

## Update: completion audit and hardening pass (2026-11-18)

Scope: audit every implemented feature, fix the concrete defects found,
re-verify, and record the authorized alternative free-model test. No live
provider success is claimed anywhere in this document.

**Backup first.** A fresh timestamped backup of the real database was taken
before any inspection or modification
(`~/.local/share/gitbot/backups/gitbot.db.pre-completion-pass.20261118-100956.bak`).
The real database was opened read-only only; `schema_migrations` shows
`0001_init`–`0007_daily_schedules` all applied (applied 2026-11-15T15:46:30Z).
The documentation in this repository still claimed `0005`–`0007` were pending;
those stale claims were corrected on 2026-11-18 (`TESTING.md`, `SECURITY.md`,
`TROUBLESHOOTING.md`, `README.md`). No migration, commit, push, pull request,
merge, or repository-settings change was made on the real profile.

**Authorized alternative free-model test — performed once, failed closed.**
Per the authorized plan, one isolated non-interactive call was made through the
host `opencode` binary with `thinkingmachines/inkling-2-small:free`
(`opencode run --standalone --format json`). Result: exit `1`, one
`provider.no-route` error — `Model unavailable: thinkingmachines/inkling-2-small:free`
(recorded in `/tmp/opencode/isolated_test_2026-11-18.ndjson`). No retry was
issued and no paid model was used. Together with the earlier findings, no free
model is verified working for this account:

| Model / route | Outcome |
| --- | --- |
| `opencode/inkling-2-small-free` (default gateway model) | OpenRouter free daily quota exhausted, 429 `free-models-per-day` (2026-11-12) |
| `opencode/big-pickle` | host-side `PONG` exit 0 (2026-11-15); sandboxed GitBot probe timed out, no output |
| `openrouter/liquid/lfm-3-1.7b:free` | manual test ended `failed` (2026-11-15) |
| `thinkingmachines/inkling-2-small:free` | isolated authorized test: `provider.no-route` (2026-11-18) |

`GET /api/health` keeps `provider_verified: false`; nothing in this pass
changes that. Health also keeps `dry_run: true`, `supervised_automation_enabled:
false`, `allow_approved_local_commits: false`, scheduler disabled by default.

**Authorized free-provider connection test through the GitBot host runner
(2026-11-18, one attempt, $0 route, no repo data):**

`opencode/inkling-2-small-free` via the GitBot `HostModelRunner` (bwrap
sandbox, `HOME=/work/home`, no credentials mounted, `HTTPS_PROXY` → relay →
CONNECT tunnel restricted to `opencode.ai:443`, strict 60 s timeout,
prompt `Reply with exactly GITBOT_PROVIDER_OK`, no tools). Result after
~3.2 s: `GatewayProviderError`, `provider_code=rate_limit`,
`http_status=429`, code `ai_gateway_rate_limited` — i.e. the `opencode.ai`
free federation has no quota for this account today either. No repository
data was sent; nothing retried; no other model was attempted; no production
configuration was changed; the production database was untouched.

`openrouter/openrouter/free` appears in the installed OpenCode catalog but is
not usable under this isolation design: the gateway tunnel allows only
`opencode.ai`/`*.opencode.ai:443` and the host model runner carries no
OpenRouter credential (deliberately). Using it would require extending the
tunnel CONNECT allowlist to `openrouter.ai:443` **and** placing the stored
OpenRouter key inside the host-runner sandbox — a security-boundary change
that was not authorized. The stored OpenRouter credential remains only in the
host-side OpenCode credential store, untouched.

**Blocker (unchanged in kind):** the account's free-inference quota is
exhausted/unavailable for both the opencode.ai federation (429 today) and, by
the earlier evidence, OpenRouter's free tier (429, 2026-11-12). No free route
is available; no paid call was made. A live `analyzed` task is impossible
until the account has free quota again or the user authorizes a boundary
change (tunnel allowlist + host-runner credential placement) in writing.

**Defects found by the read-only audit and fixed in this pass** (all covered by
new regression tests; full suite: **533 passed**):

- Committing with an expired approval (expiry between human `Approve` and
  `Commit`) destroyed the task and rolled back the applied patch. The commit
  path now pre-flights the approval (existence, `used_at`, `expires_at`,
  task/repo binding, action, diff hash) *before* any repository mutation and
  raises `ApprovalExpired`/`ApprovalInvalid`, which the API maps to 403. The
  task stays `approved`, the working tree stays untouched, and the approval is
  not consumed. `tests/unit/test_api_tasks.py::test_commit_with_expired_approval_is_refused_without_touching_the_repo`.
- `HEAD` could move between the pre-commit check and `git commit`, parenting
  the commit on an unreviewed revision. HEAD is now re-checked immediately
  before `git commit`; on mismatch the applied patch is rolled back and the
  task fails with `ApprovalMismatch`. `test_commit_refuses_when_head_moved_after_approval`.
- A dry-run write task selected by the scheduler was re-skipped on every
  30 s tick forever (queue churn, unbounded event log, stale
  `next_scheduled_at`). Skipped write tasks now have `scheduled_at` cleared and
  are never re-selected; they remain queued and can be run manually once
  dry-run is lifted. `tests/unit/test_scheduler.py` (updated) and
  `tests/unit/test_api_tasks.py::test_scheduler_tick_skips_change_tasks_in_dry_run`.
- `release_claim` could overwrite a newer claim (same second) or clobber
  another day's date after an edit. The release now restores only the exact
  pre-claim `(last_run_date, last_run_at)` pair it was given.
  `test_release_claim_does_not_clobber_a_newer_claim`,
  `test_release_claim_restores_pre_claim_state_when_untouched`.
- A patch touching `.git` metadata / `.gitattributes` could reach
  `awaiting_review` and be approved (then only fail at commit). Such patches
  are now rejected before an approval is created (task fails with a clear
  reason) and the approve endpoint refuses them with 409 as defense in depth.
  `test_write_task_with_git_metadata_patch_fails_before_review`,
  `test_approve_refuses_a_patch_writing_git_metadata`.
- The redactor did not match OpenAI-style `sk-...` keys echoed by providers, so
  a provider error string could pass a credential to the sandbox. A
  `openai_style_key` rule was added (plus tests). `tests/test_redact.py`.
- `assert_no_filter_drivers` now passes `-c core.fsmonitor=false` and
  `-c core.hooksPath=/dev/null` like the other git invocations.
- `GitRepo.diff(include_untracked=True)` silently ran `git add -A -N` (index
  mutation in a read-only module). It now raises `PolicyViolation` instead. It
  had no callers or tests.
- Cross-process cancellation was refused, so a second process sharing the
  database could never stop a run owned elsewhere. The engine now runs a
  watchdog thread for every active run; any process may record a durable
  `task.cancel_requested` event, which the owning process observes within
  ~1 s and SIGTERMs the sandbox. The API records instead of raising when the
  task is owned by another process, and requests older than the run's
  `started_at` are ignored. `tests/unit/test_tasks/test_cross_process_cancel.py`.

**Verification in this pass:** `.venv/bin/python -m pytest -q` → **533 passed,
1 warning**; `ruff check .` and `ruff format --check .` clean; `compileall`
clean; `node --test tests/frontend/dashboard_form.test.mjs` → **22 passed**;
`git diff --check` clean; `tests/unit/test_tasks/test_cross_process_cancel.py`
covers cross-process cancellation end to end. A disposable-profile `--check`
migrated a fresh DB through `0001`–`0007` and reported the sandbox available; a
disposable `--serve`
profile answered `/api/health` (dry-run defaults, `provider_verified: false`),
returned 401 for unauthenticated reads, served the dashboard (200), and
exercised the schedule API end to end (register → create → list → pause →
resume → history → delete → tick). The disposable profiles were then removed.
The real profile was not started.

**Still blocked / not done:**

- All completed work was committed locally as one coherent commit on `main`
  (not pushed; `chat.py` deliberately excluded). The production database
  remains unchanged and unmigrated by this pass.
- No live provider call has succeeded. Blocker 1 remains open. Do not weaken
  either sandbox to work around it; do not put provider credentials into
  `/work`, argv, environment variables, or snapshots.
- Cross-process cancellation is now honoured: a second process sharing the
  SQLite database records a durable `task.cancel_requested` event and the
  owning process's watchdog observes it within ~1 s, then SIGTERMs the sandbox
  (`engine._watch_cancel_requests`, `cancel_task`). Requests older than the
  run's `started_at` are ignored. Startup recovery still fails stale `running`
  rows and revokes interrupted-commit approvals. Covered by
  `tests/unit/test_tasks/test_cross_process_cancel.py`.
- The scheduler is process-local and interval-based; there is no cron parser,
  no multi-process scheduler coordination, and no reminders. Scheduled tasks
  run through the same engine and stop at `awaiting_review`; they never approve
  or commit their own changes.
- `/api/health` is intentionally unauthenticated and loopback-bound; it reports
  the local database path and version. It discloses nothing about allowlisted
  repositories or task data.
- `GitRepo` itself performs no allowlist check (callers must re-resolve); a
  defense-in-depth assertion is a possible future hardening.
- Expiry-vs-commit remains single-process; there is no cross-process approval
  revocation signal beyond the SQLite rows.

## Update: final offline release-engineering pass (2026-11-15)

Readiness classification: **B — ready for supervised local testing in dry-run
with a disposable profile.** Not A (live AI is unverified) and not C/D (no
production claims). The real profile is **not** to be started until pending
migrations (`0005`–`0007`) are approved with a backup; use `scripts/run_test_profile.sh`.

## Update: supervised daily automation completion pass (2026-11-15)

Added narrowly scoped supervised-mode controls (defaults off) to complete the
offline, review-gated daily automation workflow:

- `GITBOT_SUPERVISED_AUTOMATION_ENABLED` lets the scheduler execute existing
  sandboxed write lanes while global `GITBOT_DRY_RUN=true`, producing diffs/test
  results and moving tasks to `awaiting_review`. No auto-approval, no
  auto-commit.
- `GITBOT_ALLOW_APPROVED_LOCAL_COMMITS` (together with supervised automation)
  allows only an explicitly user-approved, diff-bound local commit while dry-run
  remains enabled. Repository `allow_commit`, approval integrity, test/diff
  guards, and the commit journal/recovery path still apply. No push/PR/merge.
- Scheduler dry-run skipping is narrowed to non-supervised write runs; the task
  engine enforces the same supervised/commit guards. Health, `--check`, API
  state, and the dashboard expose/report the new flags and warnings.

Automated evidence (disposable DB/repos, mocked AI/fake runners, no live AI,
no real profile): `pytest -ra` **519 passed, 1 warning**; `node --test
tests/frontend/dashboard_form.test.mjs` **22 passed**; `ruff check backend tests`
and `ruff format --check backend tests` clean; `compileall backend tests scripts`
clean; `git diff --check` clean. New supervised coverage includes
`tests/integration/test_supervised_daily_automation.py` (schedule -> sandboxed
execution -> diff/tests -> awaiting review -> explicit approval -> local commit,
plus no-auto-commit and commit-blocked-without-approved-local-commit cases).

Disposable-profile smoke (`scripts/run_test_profile.sh --check` with a temp
profile) applied migrations `0001_init`–`0007_daily_schedules`; temporary
`--serve` verified health/dashboard/scheduler-disabled/dry-run defaults and
authenticated schedule endpoint behavior in isolation, then the temp profile was
removed. The real DB/token were not touched or migrated; live AI/provider
verification remains `false`.

Fixed and covered by regression tests in this pass:

- **Authentication on sensitive reads** — repository paths, task prompts,
  outputs, diffs, approvals, audit rows, git status/diff/preflight, scheduler
  status, and all GitHub account data now require the bearer token. Only
  `GET /api/health` remains unauthenticated. The dashboard sends the token for
  those reads and shows a "connect token" state (without erasing task
  descriptions) on a missing or rejected token.
- **Lint scope** — the untracked root scratch script `chat.py` is excluded from
  Ruff via an explicit, commented `pyproject.toml` entry instead of being
  modified or deleted. `backend/` and `tests/` remain fully checked; root-level
  `ruff check .` and `ruff format --check .` now pass.
- **Sandbox extra-mount validation** — both source and destination are validated.
  Writable extras are limited to private `gitbot-*`/`*.sock` Unix sockets owned
  by the current user; read-only extras are limited to the relay script, fixed
  TLS/system files, or an executable mounted under `/run/gitbot/bin`. Home
  directories, repositories, credential stores, and arbitrary host paths are
  rejected; directories cannot be mounted writable.
- **Snapshot Git hardening** — snapshot `git status`/`git archive` run with
  `-c core.fsmonitor=false -c core.hooksPath=/dev/null` (plus pager/gc/maintenance
  overrides), `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_ATTR_NOSYSTEM=1`, and `GIT_OPTIONAL_LOCKS=0`. A repository-controlled
  fsmonitor/hook can no longer execute during snapshot preparation.
- **Unused host execution path** — `OpenCodeAdapter.run` now raises
  `PolicyViolation` unless a caller explicitly passes `allow_unsandboxed=True`.
  Task lanes use only the sandboxed gateway; the existing structural test that
  the engine never calls `run` still passes.
- **Logging** — `GITBOT_LOG_LEVEL` is applied to the root logger, and every root
  handler (including pre-existing uvicorn/test handlers) receives the redaction
  filter. The fully formatted record is redacted, so credentials passed through
  `%s` arguments are removed too.
- **Commit crash safety** — commit now journals `task.commit_started` before
  mutating the index, consumes the approval only after the exact reviewed diff is
  staged, and records `task.committed`/`task.commit_failed`. Startup recovery
  fails an interrupted commit and revokes its approval, so a stale approval can
  never be replayed. Exact-diff approval is unchanged.
- **Disposable profile** — `scripts/run_test_profile.sh` overrides
  `HOME`/`XDG_DATA_HOME`/`XDG_CONFIG_HOME`, drops inherited `GITBOT_*` values,
  refuses the real data/config paths, never copies the real token, and defaults
  to `127.0.0.1:8766`, `dry_run=true`, scheduler disabled, model unchanged.
  Integration tests prove migrations `0001`–`0006` apply only to the throwaway
  DB, task/approval/history/restart recovery works there, and the real DB mtime
  and size do not change.

Verification for this pass:

- `.venv/bin/python -m pytest -q` — **497 passed, 1 warning** (the upstream
  Starlette/httpx TestClient deprecation).
- `node --test tests/frontend/dashboard_form.test.mjs` — **20 passed**.
- `.venv/bin/python -m pytest -q tests/test_dashboard_frontend.py` — 1 passed.
- `.venv/bin/ruff check backend tests` and `ruff check .` — clean.
- `.venv/bin/ruff format --check backend tests` and `.` — clean.
- `.venv/bin/python -m compileall -q backend tests` — clean.
- `git diff --check` — clean.

Still blocked or incomplete:

- A live provider call through the gateway has **not** been run; health keeps
  `provider_verified: false`. The host runner sandbox has no provider credential
  store (`HOME=/work/home`), so live AI currently fails closed. Supplying
  credentials is a user decision and must not be done via argv, environment
  variables, `/work`, or snapshots.
- The real profile still has only migrations `0001`–`0004`. `0005` and `0006`
  remain unapplied pending a backup and explicit approval. The startup path still
  migrates unconditionally, so do not run the real profile yet. (superseded —
  `0005`–`0007` were applied on 2026-11-15 after a backup; see the 2026-11-18
  completion audit entry.)
- Cross-process cancellation, cron parsing, multi-process scheduler
  coordination, and reminders remain unimplemented. (partially superseded —
  cross-process cancellation is honoured since 2026-11-18.)
- No live AI/API call, real database migration, GitHub write, commit, push, pull
  request, or merge was performed in this pass.

## Update: MVP audit and security hardening (2026-11-15)

Fixed and covered by regression tests in this pass:

- **Git diff argument injection** — `GET /api/repos/{id}/git/diff?base=...` no
  longer passes a caller-controlled value directly to `git diff`. Option-like
  values and values containing whitespace/control characters are rejected with
  `422`, and `--` terminates the revision/path arguments. A crafted
  `--output=<file>` can no longer write outside the repository.
- **Execution-time repository policy** — the engine re-checks the repository
  allowlist and `allowed_task_types` immediately before a queued task runs. A
  task queued before a policy was tightened now fails closed instead of running.
- **Approved-task cancellation** — cancelling an `approved` task now revokes its
  unused approval and blocks a later commit. Commit and approved-cancel share a
  process lock so the two cannot race over the same approval.
- **Retry lineage** — a failed task can create at most one retry child. Repeating
  the same retry request is refused, preventing the parent task from bypassing
  the retry budget; retry the newest failed task when budget remains.
- **Task-event redaction** — both engine and scheduler task events pass through
  the same credential-shape redactor as audit rows, including failure messages.
- **Migration ordering** — the runner refuses to proceed when an applied
  migration file is missing or when a pending migration would be applied out of
  order. It still does not apply migrations to the real profile.
- **Patch metadata coverage** — reviewed patches that rename/copy `.git`
  metadata or reach it only through a source/rename header are rejected in
  addition to the existing `.git` and `.gitattributes` checks.
- **Approval binding** — approval endpoints now require the approval row to be a
  commit approval and to be the approval currently attached to the task.
- **Dashboard correctness** — validation details are surfaced instead of a
  generic "request validation failed"; failed background refreshes preserve the
  last known rows; scheduler ticks report processed/failed/skipped counts;
  approved tasks expose a cancel action; approved tasks without a passed
  sandboxed test command show an explicit warning.

Verification for this pass:

- `.venv/bin/python -m pytest -q` — **469 passed, 1 warning** (the warning is
  the upstream Starlette/httpx TestClient deprecation).
- `.venv/bin/ruff check backend tests` — clean.
- `.venv/bin/ruff format --check backend tests` — clean.
- `.venv/bin/python -m compileall -q backend tests` — clean.
- `node --test tests/frontend/dashboard_form.test.mjs` — 17 passed, 0 failed.
- `git diff --check` — clean.

Still blocked or incomplete:

- A live provider call through the gateway has **not** been run; health keeps
  `provider_verified: false`.
- The real profile still has only migrations `0001`–`0004`. `0005` and `0006`
  remain unapplied pending a backup and explicit approval. (superseded —
  `0005`–`0007` applied 2026-11-15 after a backup; see the 2026-11-18 entry.)
- Cross-process cancellation, cron parsing, multi-process scheduler
  coordination, and reminders remain unimplemented. (partially superseded since
  2026-11-18.)
- No live AI/API call, real database migration, credential read, commit, push,
  pull request, or merge was performed in this pass.

## Update: host AI gateway and sandbox relay (2026-11-12)

Implemented and covered by tests since the previous pass:

- **Authenticated host gateway** — `gitbot.opencode.gateway` exposes an
  OpenAI-compatible endpoint on a private Unix socket with a per-run capability
  token, constant-time comparison, request/prompt/output bounds, and tool-name
  validation. Provider errors are redacted before returning to the sandbox.
- **Sandbox relay** — `gitbot.security.sandbox_relay` is a stdlib byte pump
  mounted read-only into the repository sandbox. It binds loopback only and
  forwards to the bind-mounted gateway socket; it parses no HTTP and runs a
  fixed argv.
- **Second host sandbox** — the host OpenCode model runner runs in its own
  Bubblewrap sandbox with no repository, no host home/credentials, and only an
  allowlisted `CONNECT` tunnel to `opencode.ai`/`*.opencode.ai:443`.
- **Policy integration** — the gateway provider and model are written into the
  sandbox policy and verified with `opencode debug config` inside the sandbox.
  The capability token is hidden from repr output and never audited.
- **Health and configuration** — `GITBOT_AI_GATEWAY_ENABLED`,
  `GITBOT_AI_GATEWAY_MODEL`, `GITBOT_AI_GATEWAY_TIMEOUT_S`, and
  `GITBOT_AI_GATEWAY_RELAY_PORT` are documented in `.env.example`; health shows
  gateway status and keeps `provider_verified: false`.
- **Tests** — new unit coverage for gateway config, normalization, prompt and
  decision parsing, HTTP auth/routes/limits/streaming/redaction, lifecycle,
  tunnel allowlisting, runner failure mapping, and gateway policy rendering and
  verification. New integration tests use real Bubblewrap and a real relay to
  prove loopback→Unix-socket forwarding, host-network isolation, and an
  end-to-end authenticated chat request through the gateway with a fake runner.

Verification for this pass: **343 passed, 1 warning**; Ruff lint/format,
`compileall`, `git diff --check`, and the Node dashboard suite are clean. See the
final report for exact commands.

Still blocked or incomplete:

- A live provider call through the gateway has **not** been run by the automated
  suite. Health intentionally reports `provider_verified: false`.
- Migrations `0005` and `0006` are still not applied to the real profile.
- Cross-process cancellation, cron parsing, multi-process scheduler
  coordination, and reminders remain unimplemented.

## Update: hardening, cancellation, approvals, dashboard status (2026-11-05)

Implemented and covered by tests since the previous pass:

- **Late-cancellation barrier** — cancellation is re-checked after the model
  exits and after sandboxed tests finish, and again by the engine before a task
  can move to `awaiting_review`. A cancel can no longer silently produce an
  approval.
- **Actionable approvals only** — `GET /api/tasks/approvals/pending` now joins
  tasks and excludes unused-but-expired approvals and tasks that are no longer
  awaiting review. Rejecting after a task has left review returns `409` instead
  of falsely reporting success or consuming the approval.
- **Commit failure rollback** — if applying the reviewed patch or creating the
  local commit fails, GitBot reverses its own patch to restore the clean working
  tree and reports a failed rollback explicitly. The approval is still burned.
- **Live Git hardening** — Git commands now use a filtered environment, so
  `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` cannot redirect operations.
  Command-line configuration disables repository hooks, `core.fsmonitor`, GPG
  signing, pager execution, and automatic maintenance. Diffs disable external
  diff and textconv commands.
- **Git metadata and filter rejection** — worktree `.git` files are accepted only
  when their target exists and remains inside the allowed roots; symlinked
  `.git` metadata is rejected. Repositories whose attributes select a Git filter
  driver are refused, and reviewed patches cannot write `.git` metadata or add, modify, or rename a
  `.gitattributes` file. This closes a repository-controlled command-execution
  path found during the audit.
- **Dashboard system status** — a live panel reports server/dry-run state,
  database path and migrations, sandbox capabilities, OpenCode/AI toolchain,
  scheduler, repository/task counts, and pending approvals. It states that the
  AI *toolchain* is ready without claiming a model route has been verified.
- **Dashboard correctness** — dry-run text now says read-only tasks still run;
  the create-task hint no longer claims a dedicated `gitbot/<task>` branch; the
  repository status panel no longer removes the policy panel; commit is disabled
  and explained when tests failed/errored, the diff was truncated, dry-run is on,
  or the token is missing.

Verification for this pass: **276 passed, 1 warning**; Ruff lint/format,
`compileall`, `git diff --check`, and the Node dashboard suite are clean.
`gitbot --check` with a throwaway HOME applied migrations `0001`–`0006` and
reported the sandbox available. The real profile was opened read-only and still
reports only `0001`–`0004`. (superseded — `0005`–`0007` were applied on
2026-11-15; see the 2026-11-18 completion audit entry.)

Genuine AI evidence:

- Host-side OpenCode inference works: `opencode run --standalone --format json
  --model opencode/big-pickle` returned `PONG` with exit 0. This is **not** proof
  of sandboxed GitBot execution.
- A GitBot sandboxed analysis probe using the same model timed out with no
  output; the source repository's HEAD and working tree remained unchanged.
  This is consistent with the deliberate network isolation and keeps blocker 1
  open.

Still blocked or incomplete:

- Real AI execution through GitBot remains unavailable (blocker 1).
- Migrations `0005` and `0006` are still not applied to the real profile.
- Git filter drivers are deliberately unsupported, so repositories that rely on
  them are refused.
- Cross-process cancellation, cron parsing, multi-process scheduler
  coordination, and reminders remain unimplemented.

## Update: explain, task policy, scheduler, retries, tests, contributions (2026-11-04)

Implemented and covered by tests since the previous pass:

- **Explain tasks** — a read-only lane alongside analyze. It uses the same
  snapshot sandbox, finishes as `explained`, and produces no diff, approval, or
  commit. Both read-only types are allowed while dry-run is enabled.
- **Per-repository task policy** — `allowed_task_types` can be changed through
  the API or the dashboard **Policy** panel; the engine refuses a disabled type.
- **Scheduler and retries** — opt-in background scheduler
  (`GITBOT_SCHEDULER_ENABLED=false` by default) for queued tasks with a due
  `scheduled_at`. A single-process lock prevents duplicate ticks; write tasks are
  skipped (and logged) while dry-run is enabled. Retries create a fresh queued
  task within a per-task budget of 0–5 and record the new task ID.
- **Sandboxed test execution** — an optional repository `test_command` is split
  with `shlex`, shell operators are rejected, and the fixed argv runs inside the
  disposable workspace after the model run. Output is bounded/redacted, the exit
  code and status are stored, and failed/errored tests block commit.
- **Read-only contribution tracking** — `GET /api/github/contributions` returns a
  date-bounded calendar and commit/issue/PR/review breakdown through the existing
  `gh` login. No artificial activity is created.
- **Health and dashboard** — health reports sandbox, OpenCode, AI readiness, and
  scheduler status. The dashboard exposes explain, task policy/test command,
  scheduling/retries, test results, retry actions, scheduler tick, and the
  contribution summary.
- **Migration `0006_explain_scheduler_tests.sql`** — adds the `explained`
  state/type, repository task policy and test command, scheduling/retry fields,
  and test-result fields. It is tested on fresh and upgraded temporary databases
  only.

Verification for this pass: **261 passed, 1 warning**; Ruff lint/format, compile,
whitespace, and Node dashboard tests clean.

Still blocked or incomplete:

- Real AI execution remains unavailable for the reasons in blocker 1.
- Migrations `0005` and `0006` have **not** been applied to the real profile.
- The scheduler is interval-based and process-local; there is no cron parser or
  cross-process coordination.
- Cancellation of a run owned by another process is still refused. (superseded
  2026-11-18 — cross-process cancellation is now honoured; see the completion
  audit entry.)
- Sandboxed tests are only as good as the configured command; they are not
  inferred from repository content.

## Update: sandboxed Analyze and code-change lanes

Both lanes now execute through the Bubblewrap sandbox with a disposable
`git archive HEAD` snapshot.

- **Analyze** mounts the snapshot read-only and allows reads only. It ends in the
  terminal `analyzed` state with no branch, diff, approval, or commit.
- **Code change** copies the snapshot into a private writable `/work/repo` with
  no `.git`, allows file edits only there, and denies shell/execute. A host-only
  bare git repository outside the sandbox mount records the baseline, so a model
  cannot rewrite refs or the index to hide a change. The generated patch is
  stored and hashed for the existing approval flow.
- **Approval** checks the stored hash plus the live branch, `HEAD`, and clean
  working tree. **Commit** re-verifies all of that, applies the exact stored
  patch to the index, checks the applied diff hash, then creates a local commit.
  Symlink and submodule changes are rejected. There is no live-repository
  execution fallback.
- Analyze tasks are allowed while dry-run is enabled; code-change execution and
  commits remain blocked.

Evidence:

- `tests/integration/test_analysis_lane.py` runs the real Bubblewrap sandbox
  with a fake OpenCode binary (policy verification, no repository writes, output
  bounds, process errors, and cancellation).
- `tests/integration/test_change_lane.py` verifies accurate new/modified/deleted
  and binary diffs, source-repository immutability, temporary workspace cleanup,
  symlink rejection, and fail-closed behavior without Bubblewrap.
- `tests/integration/test_task_engine_fake_opencode.py` verifies no live adapter
  call, cancellation, missing-Bubblewrap failure, and preserved repository state.
- Migration `0005_analysis_state.sql` adds `analyzed` and sandbox metadata. It
  is written and tested against fresh and populated databases but **has not
  been applied to the real user profile**.
- Full suite: **242 passed** at the time of the sandbox pass; see the
  2026-11-04 update above for the current **261 passed** result.

This does not change the verdict: both lanes are verified with a fake OpenCode
binary, not a real model route. Code-change execution is sandboxed, but no real
AI execution has been demonstrated and the project is not production-ready.

## Safety position for this audit

- `GITBOT_DRY_RUN` remained `true` throughout.
- No repository was added to GitBot's own allowlist.
- `/home/om/whatsapp-message-sender` and `/home/om/gitbot-test-repo` were not
  modified.
- No database was migrated, reset, or deleted on the real profile. App smoke
  tests used a throwaway `HOME`, database, token, and repository under
  `/tmp/opencode/gitbot-smoke/`.
- No dependency, model, package, or system component was installed.
- No commit, push, pull request, or merge was performed.

## Verified working

| Area | Evidence |
| --- | --- |
| Config, loopback-only bind, dry-run default | `uv run gitbot --check` |
| Migrations `0001`–`0005` on a fresh database | `--check`, pytest DB tests |
| Repository allowlist and path validation | pytest, live API |
| GitHub read-only status/repository listing | pytest with fake `gh` |
| Task create/list/detail/cancel and audit | live API on `127.0.0.1:8876` |
| Dry-run refusal leaves a task queued | live API returned HTTP 403 |
| Approval diff-hash checks and failure paths | pytest |
| OpenCode policy generation/verification | pytest with fake and real OpenCode |
| Dashboard static page and form behaviour | Node VM harness, 14 cases |
| Bubblewrap availability | `bubblewrap 0.11.1`, unprivileged user namespaces |
| Sandbox host isolation | real bwrap tests: `/repo` writes, `/home`, `/root`, `/etc`, `/etc/shadow`, and network denied; `/work` writes allowed |
| OpenCode runtime confinement | real OpenCode `debug paths` reports only `/work` paths |
| Sandbox timeout, cancellation, output cap | real bwrap tests |
| Sandboxed code-change execution | real bwrap + fake OpenCode: source untouched, workspace-only writes, accurate patch |
| Approval bound to the sandbox-generated diff | pytest API approval/commit tests |
| Commit applies only the exact reviewed patch | pytest API commit tests |
| Process-group cancellation escalation | fake OpenCode ignores SIGTERM; adapter escalates to SIGKILL |
| Stale-run recovery | tasks left `running` are failed on startup with an audit/event trail |
| Startup recovery | API lifespan records `app.recover_stale` and does not crash on recovery errors |

Commands used for the final verification pass:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q backend tests
git diff --check
```

Observed result for the sandbox pass: **242 passed, 1 warning**; the
2026-11-04 update superseded this with **261 passed, 1 warning**, and the
2026-11-05 pass reports **276 passed, 1 warning**. Lint, format, compile, and
whitespace checks are clean.

## Changes made in this pass

1. Added `run_sandbox_command` with hard timeout, cancellation watchdog,
   process-group SIGTERM/SIGKILL escalation, and bounded stdout/stderr.
2. Added `--unshare-user` and `--disable-userns` to the sandbox probe and
   command, preventing nested user namespaces.
3. Added a reserved-environment block so callers cannot override `HOME`,
   `PATH`, `TMPDIR`, XDG paths, or Git safety variables.
4. Exported the sandbox runner and result type from `gitbot.security`.
5. Escalated OpenCode adapter cancellation from SIGTERM to SIGKILL after the
   configured grace period.
6. Rejected duplicate concurrent executions of the same task in one process.
7. Added startup recovery for rows left `running` by a crashed server; recovery
   fails the task, records `task.recovered.stale` and `app.recover_stale`, and
   leaves the working tree untouched.
8. Disabled **Create & run** in the dashboard while dry-run is active or while
   disconnected, and added a server-side-equivalent guard before the create
   request. Task descriptions are still never cleared automatically.
9. Bounded task prompt/model input and rejected `bool`/non-positive repo ids.
10. Added `sandbox:` status to `gitbot --check`.
11. Corrected the README's security/status claims so it no longer implies tasks
    run inside the sandbox or that offline AI works.
12. Integrated the sandbox into `TaskEngine` code-change execution: writable
    `/work/repo` copy, host-only baseline, sandboxed policy check, bounded diff,
    symlink/submodule rejection, no live fallback, and exact-patch commit apply.

## Release blockers

### 1. Live AI execution is implemented but not verified in the automated suite

Observed and implemented:

- The repository sandbox still has no direct network route.
- A host-side, authenticated gateway now provides the narrowly scoped broker
  path: loopback relay → Unix socket → validated OpenAI-compatible request →
  second Bubblewrap sandbox for the host OpenCode runner → allowlisted
  `*.opencode.ai:443` CONNECT tunnel.
- Gateway authentication, request limits, tool validation, policy verification,
  redaction, streaming, and failure paths are covered by unit and real-Bubblewrap
  integration tests with fake runners.
- A live provider call has not been run in the test suite, so health keeps
  `provider_verified: false`.

Do not "fix" any remaining live-route failure by giving the repository sandbox
unrestricted network access or by pasting credentials into `/work`. To verify:

1. Ensure the host `opencode` binary can run the configured gateway model.
2. Run one analyze task with `GITBOT_AI_GATEWAY_ENABLED=true`.
3. Confirm the task reaches `analyzed` and health shows the gateway enabled.
4. If it fails, inspect redacted logs; do not weaken either sandbox.

No provider credential is read by GitBot or written into the repository sandbox.

### 2. Code-change execution is sandboxed but not demonstrated with a real model

`TaskEngine.run_task` no longer executes code-change tasks against the live
repository. It snapshots `HEAD`, copies the snapshot to a private writable
`/work/repo`, runs OpenCode there, and generates the review patch from a
host-only baseline outside the sandbox mount. Commit applies that exact stored
patch to the live index only after approval.

The remaining limitations are:

- the lane is verified with a fake OpenCode binary, not a real model;
- model-initiated shell execution is denied; only the user-configured test
  command runs as a fixed argv in the disposable workspace;
- symlink and submodule changes are refused;
- diffs larger than the review limit fail the task instead of being truncated;
- legacy `awaiting_review` rows without a sandboxed `snapshot_head` cannot be
  approved or committed and must be re-run;
- applying the patch to the user's current branch can still be interrupted by a
  Git failure after the approval is consumed; GitBot now attempts to reverse its
  own patch and reports if rollback also fails;
- repositories that use Git filter drivers are rejected rather than run.

Migration `0005` supplies the `analyzed` state and sandbox metadata; it is
written and tested, but was not applied to the real database because approval was
not given.

### 3. Original-scope features: resolved and remaining

Resolved in the 2026-11-04 pass: contribution activity tracking, the opt-in
persistent scheduler, test execution as a task phase with captured results,
automatic retry creation with history, and explain tasks/per-repository policy.

Remaining gaps:

- Live cross-process cancellation. Startup recovery fails stale `running` rows,
  but a second process cannot cancel a run owned by another process. (superseded
  2026-11-18 — honoured via the database-driven cancel watchdog; see the
  completion audit entry.)
- The scheduler is process-local and interval-based; no cron syntax or
  multi-process coordination.
- Real AI execution is still unproven (blocker 1).
- Sandboxed tests depend on an explicitly configured command and are not
  discovered from repository content.

### 4. Existing database state

The real database was opened read-only during this audit. It reports migrations
`0001_init`, `0002_tasks`, `0003_task_output`, and `0004_task_review`; these were
already applied before this audit. Migrations `0005_analysis_state` and
`0006_explain_scheduler_tests` were written and tested but deliberately not
applied to the real profile. No migration was applied, reset, or edited on the
real profile, and no task/settings/audit rows were changed. Any future schema
change needs explicit approval and a backup. (superseded — `0005`–`0007` were
applied on 2026-11-15 after a backup; see the 2026-11-18 completion audit
entry.)

## Required next decisions

1. Review, back up, then approve or reject applying `0005_analysis_state` and
   `0006_explain_scheduler_tests` to the existing profile. Until then, the real
   app must not be migrated.
2. Verify the implemented host gateway against the real provider: run one
   analyze task with `GITBOT_AI_GATEWAY_ENABLED=true` and confirm it reaches
   `analyzed` without weakening either sandbox.
3. Approve the database backup/migration procedure before starting the real app
   against the existing profile.
4. Decide which explicit test command (if any) is appropriate for each
   allowlisted repository before enabling code-change tasks outside dry-run.

## Exact next step

The safest next milestone is to prove the live AI route without weakening the
sandbox:

- keep `GITBOT_DRY_RUN=true`;
- confirm the host `opencode` binary can run `GITBOT_AI_GATEWAY_MODEL`;
- run one end-to-end analyze task through the gateway and real Bubblewrap;
- confirm the source repository is untouched, the gateway returns a bounded
  answer, and health reports the gateway enabled;
- then run one code-change task and confirm the patch is accurate and
  approval/commit still bind to the exact diff;
- only then consider applying migrations `0005` and `0006` to the real profile
  after a backup and explicit approval.

Do not describe the project as production-ready until live execution and the
migration state are resolved.
