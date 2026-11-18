# Safe Local Testing Guide

This guide is for running GitBot on your own machine while it is still a
local-first MVP. The real profile now has migrations `0001`–`0007` applied (on
2026-11-15, after a backup was taken; see `docs/RELEASE-READINESS.md`). The
disposable-profile section below stays the recommended way to try changes
first: it keeps the real `~/.local/share/gitbot` database untouched.

## 1. Installation and dependencies

Required on the host:

- Python 3.14 (the project is developed and tested on 3.14).
- `git`.
- `gh` (GitHub CLI), authenticated with `gh auth login`, for GitHub reads.
- Bubblewrap (`bwrap`) with user namespaces enabled for the strong sandbox.
- OpenCode on `PATH` for real-AI runs. Without it, health reports the toolchain
  as unavailable and task execution fails closed.

Project setup:

```bash
cd /home/om/GitBot
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff --version
.venv/bin/pytest --version
```

Do not install system packages as part of a GitBot task. The sandbox fails
closed when `bwrap` or user namespaces are unavailable; there is no unsandboxed
fallback.

## 2. Configuration

Defaults (all validated at startup):

| Variable | Default | Notes |
| --- | --- | --- |
| `GITBOT_HOST` | `127.0.0.1` | Non-loopback values are rejected at startup. |
| `GITBOT_PORT` | `8765` | The test-profile script uses `8766`. |
| `GITBOT_DRY_RUN` | `true` | Global dry-run remains enabled by default. |
| `GITBOT_SUPERVISED_AUTOMATION_ENABLED` | `false` | Scheduler may execute sandboxed write lanes to awaiting review only; no auto-approve/commit. |
| `GITBOT_ALLOW_APPROVED_LOCAL_COMMITS` | `false` | With supervised automation, allows an explicitly approved local commit while dry-run stays enabled. |
| `GITBOT_SCHEDULER_ENABLED` | `false` | Background scheduling is opt-in. |
| `GITBOT_AI_GATEWAY_MODEL` | `opencode/inkling-2-small-free` | Do not change it for this phase. |
| `GITBOT_LOG_LEVEL` | `INFO` | Applied to the root logger; logs are redacted. |

To exercise supervised automation in a disposable profile, keep `GITBOT_DRY_RUN=true` and set `GITBOT_SUPERVISED_AUTOMATION_ENABLED=true`. To allow the final explicit local commit from that same dry-run profile, also set `GITBOT_ALLOW_APPROVED_LOCAL_COMMITS=true`. Leave both `false` on the real profile unless you explicitly decide otherwise. No automatic commit, push, PR, merge, or fake timestamps occur.

Paths come from `XDG_DATA_HOME` and `XDG_CONFIG_HOME`:

- database: `<XDG_DATA_HOME>/gitbot/gitbot.db`
- token: `<XDG_CONFIG_HOME>/gitbot/token` (`0600`, never logged)

Never put a token in a URL, a command argument, a repository, or a commit.

## 3. Disposable test profile

The real profile has migrations `0001`–`0007` applied (2026-11-15, after a
backup). To try anything new safely, use a throwaway profile.

The script `scripts/run_test_profile.sh` isolates every path:

```bash
# Migrate and validate a fresh throwaway database only.
scripts/run_test_profile.sh --check

# Start the dashboard against that throwaway profile.
scripts/run_test_profile.sh --serve
```

It sets, for the child process only:

- `HOME=<profile>/home`
- `XDG_DATA_HOME=<profile>/data`
- `XDG_CONFIG_HOME=<profile>/config`
- `GITBOT_HOST=127.0.0.1`
- `GITBOT_PORT=8766`
- `GITBOT_DRY_RUN=true`
- `GITBOT_SCHEDULER_ENABLED=false`

It drops inherited `GITBOT_*` variables, refuses to run when the profile points
at the real data/config directories, and never copies the real token. The
default profile root is `/tmp/gitbot-test-profile`; override it with
`GITBOT_TEST_PROFILE=/path/to/profile`. Delete that directory to reset the test
profile.

`scripts/run_test_profile.sh` does not enable supervised automation. For a
supervised disposable run, start with an equivalent explicit environment that
keeps dry-run enabled and adds only the narrow flags you need, for example:

```bash
TEST_PROFILE=/tmp/gitbot-test-profile
mkdir -p "$TEST_PROFILE"/{home,data,config}
HOME="$TEST_PROFILE/home" \
XDG_DATA_HOME="$TEST_PROFILE/data" \
XDG_CONFIG_HOME="$TEST_PROFILE/config" \
GITBOT_HOST=127.0.0.1 GITBOT_PORT=8766 \
GITBOT_DRY_RUN=true GITBOT_SUPERVISED_AUTOMATION_ENABLED=true \
GITBOT_ALLOW_APPROVED_LOCAL_COMMITS=true GITBOT_SCHEDULER_ENABLED=false \
.venv/bin/python -m gitbot.main --serve
```

Equivalent explicit command (if you prefer not to use the script):

```bash
export TEST_PROFILE=/tmp/gitbot-test-profile
mkdir -p "$TEST_PROFILE"/{home,data,config}
HOME="$TEST_PROFILE/home" \
XDG_DATA_HOME="$TEST_PROFILE/data" \
XDG_CONFIG_HOME="$TEST_PROFILE/config" \
GITBOT_HOST=127.0.0.1 GITBOT_PORT=8766 \
GITBOT_DRY_RUN=true GITBOT_SCHEDULER_ENABLED=false \
.venv/bin/python -m gitbot.main --serve
```

### Exact migration verification for the throwaway DB

```bash
PROFILE=/tmp/gitbot-test-profile
.venv/bin/python - "$PROFILE" <<'PY'
import sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1]) / "data" / "gitbot" / "gitbot.db"
with sqlite3.connect(db) as connection:
    for (version,) in connection.execute(
        "select version from schema_migrations order by version"
    ):
        print(version)
PY
```

Expected output: `0001_init` through `0007_daily_schedules`.

**Do not run `gitbot --check` or `gitbot --serve` against the real profile
until any new migration decision is explicitly resolved.** Every new migration
beyond those already applied to the real profile requires a backup and
explicit approval. Both commands migrate the configured database on
startup.

## 4. Dashboard URL and authentication

- Dashboard: <http://127.0.0.1:8766/dashboard> for the test profile
  (8765 for a normal profile).
- Health: `GET /api/health` is the only unauthenticated data/diagnostic read.
- Everything else requires `Authorization: Bearer <token>`.

The test-profile token is created on first `--serve`:

```bash
cat /tmp/gitbot-test-profile/config/gitbot/token
```

Paste it into the dashboard's token field and press **Connect**. The token is
held in memory only and sent as a bearer header for repository, task, approval,
audit, git, and GitHub reads. Do not print the token into logs or shell history
when others can see the screen.

## 5. Repository registration

1. Start the profile above.
2. Connect with the token.
3. In **Repositories**, enter an absolute path to a local Git repository **you
   own and intend GitBot to use**. The allowlist root follows the `HOME` of the
   running profile, so for the test profile place or clone a disposable test
   repository under `/tmp/gitbot-test-profile/home/projects/`. The GitBot
   project itself is always forbidden.
4. Choose the allowed task types and, optionally, a test command.
5. Leave **Allow local commit** off until you have reviewed a diff.

The server re-validates the stored path on every use and refuses paths that
disappear or leave the allowlist.

## 6. Running Analyze

1. Select the repository.
2. Task type: `analyze`.
3. Enter a read-only question, for example: "Summarize the main entry points and
   the test layout."
4. Click **Create & run** (dry-run permits read-only tasks).

Analyze runs inside Bubblewrap on a read-only snapshot. It must make zero tool
calls; an answer that claims tool use is rejected. Results end in `analyzed`.

## 7. Testing Explain

Same flow with task type `explain` (for example: "Explain how configuration is
loaded"). Explain is also read-only, allowed in dry-run, and ends in `explained`.

## 8. Testing write tasks

Write task types are `bugfix`, `feature`, `docs`, `deps`, and `tests`.

Dry-run blocks these at execution. To exercise them you must explicitly set
`GITBOT_DRY_RUN=false` in the throwaway profile, and the repository must allow
commits before a local commit is possible:

```bash
GITBOT_DRY_RUN=false GITBOT_TEST_PROFILE=/tmp/gitbot-write-test \
  scripts/run_test_profile.sh --serve
```

Write tasks still run only inside the sandbox. The sandbox produces a patch in a
disposable workspace; your repository is not modified until you approve and
commit. Empty diffs are refused and never create an approval.

## 9. Inspecting diffs

Open a task with state `awaiting_review`. The detail view shows:

- the exact stored patch and changed-file list,
- the SHA-256 diff hash,
- the snapshot HEAD and branch,
- sandboxed test status (if a test command is configured),
- whether the response or diff was truncated.

The diff shown is the stored sandbox patch, not a live host diff.

## 10. How approval works

Approval is bound to the task, repository, action, branch, snapshot HEAD, and
exact diff hash. To approve, tick the confirmation box and press **Approve
diff**. The dashboard sends the server-side hash, not a user-typed one.

Approval is refused if the working tree is dirty or the branch/HEAD/hash
changed. An approval expires after 30 minutes, is single-use, and is consumed
only after the exact reviewed patch is staged. Cancelling an approved task
revokes the approval.

## 11. How local commit works

With `GITBOT_DRY_RUN=false`, `allow_commit` enabled, and a passed test run (when
configured), press **Approve & commit locally**. GitBot:

1. re-checks the allowlist, state, hash, branch, HEAD, and clean tree,
2. applies the exact stored patch to the index,
3. verifies the staged diff hash,
4. consumes the approval,
5. creates one local commit.

It never pushes, opens pull requests, merges, or changes remote settings. If
commit fails, the patch is reversed and the task is failed. A crash during
commit is journaled (`task.commit_started`); the next startup fails the task and
revokes the approval so it cannot be replayed. When that happens, inspect
`git status` and `git log` in the repository before re-running the task.

## 12. Stopping and recovering

- Stop the server with `Ctrl+C` in the terminal running the script.
- The dashboard's token is cleared on page reload; nothing is persisted in the
  browser.
- Startup recovery fails tasks left `running` by a previous process and fails
  interrupted commits. It never edits the repository; inspect the working tree
  manually.
- If a task fails, read the redacted error in the task detail, fix the cause,
  and use **Retry** within the configured retry budget (each task can spawn one
  retry child).
- To reset the test profile completely, stop the server and delete
  `/tmp/gitbot-test-profile`.

## 13. Known limitations

- No live AI provider call has been verified. Health keeps
  `provider_verified: false`; fake-runner tests do not prove a real model route.
- The host model runner has no provider credential store in this environment.
  The design is fail-closed; supplying credentials is a separate user decision
  and must not be done by putting secrets in argv, environment variables,
  `/work`, or repository snapshots.
- The real profile has migrations `0001`–`0007` applied. Any *new* migration
  file still requires a backup and explicit approval before it runs against the
  real profile.
- Cross-process cancellation, cron parsing, multi-process scheduler
  coordination, and reminders are not implemented.
- The scheduler processes due tasks in one process; it is not a distributed
  queue.
- GitHub contribution data is informational only. GitBot does not create
  artificial commits or promise contribution-graph changes.
- Supervised automation executes sandboxed write lanes and stops at
  `awaiting_review`; it never auto-approves or auto-commits. Local commits in
  supervised dry-run mode require explicit user approval and explicit config. No
  automatic push/PR/merge exists.

## 14. Testing levels

| Level | What it means | Evidence |
| --- | --- | --- |
| Offline fake-runner testing | Hermetic pytest and Node tests with fake `gh`, fake `opencode`, and fake model runners. No provider, network, or real database. | `pytest -q`, `node --test`, disposable-profile tests. |
| Real AI testing | A real model is reached through the sandboxed gateway with the configured model. Requires provider authentication that this environment does not currently provide. | Not verified. |
| Supervised local testing | You run the dashboard on loopback with a disposable profile and review every diff/commit. | Supported now, dry-run by default. |
| Production deployment | Multi-user hardening, remote deployment, credential management, backups, and operational monitoring. | Not supported. |

GitBot is an offline MVP that is ready for supervised local testing in
dry-run. It is not production-ready and real AI integration is unverified.
