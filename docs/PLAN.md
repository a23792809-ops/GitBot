# GitBot — Implementation Plan (Milestone 1, for review)

Status: **draft, awaiting approval**. No dependencies installed, no code written,
no migrations run, no GitHub writes performed.

---

## 0. Environment findings (measured, not assumed)

| Item | Result | Impact |
|---|---|---|
| Python | 3.14.4, `/usr/bin/python3`; only 3.14 present | Bleeding edge; every binary dep must have cp314 wheels |
| pip | **missing** (`No module named pip`) | Blocks normal dependency install |
| ensurepip | **missing** | `python3 -m venv` fails at pip bootstrap |
| `python3 -m venv --without-pip` | **works** (exit 0, interpreter runs) | Escape hatch exists |
| `python3-venv` / `python3-pip` apt packages | not installed | Fixing via apt needs sudo |
| sudo | `interactive authentication is required` | **No unattended apt installs** |
| Node.js / npm | 22.23.3 / 10.9.9 | Fine for Next.js 15 |
| Git | 2.53.0 | Fine |
| GitHub CLI | 2.46.0, authenticated as `a23792809-ops` over HTTPS, scopes `gist, read:org, repo, workflow` | `repo` is broad; use as-is, do not expand |
| gh API reachability | 3 public repos, rate limit 5000 (4998 left) | Fine |
| GraphQL contributions | works: 49 total, 0 restricted | Feature 7 is feasible read-only |
| OpenCode | 2.0.27; `run --format json`, `serve --hostname/--port`, `api`, `session` | Supported integration surfaces exist |
| `@opencode-ai/sdk` | 1.19.4 on npm | Option for Tier-2 integration |
| sqlite3 CLI | **not installed**; Python `sqlite3` module = SQLite 3.46.1 | Use Python for all DB access; no CLI scripts |
| pytest / ruff / mypy / uv / pipx | all missing; network to pypi + npm = 200 | Need bootstrap decision (see §9) |
| Local git repos | `~/whatsapp-message-sender`, `~/GitBot` | Only one real target repo exists |
| Disk | 411 GB free | Fine |

cp314 wheel probe (PyPI, latest): `pydantic` 2.14.1, `pydantic-core` 2.50.1,
`fastapi` 0.162.0, `uvicorn` 0.54.0, `pytest` 9.1.1, `httpx` 0.28.1 all publish
cp314 or pure-Python wheels. `ruff` 0.17.2 ships `py3-none-manylinux` wheels, which
install on 3.14.

**Conclusion:** the stack is viable, but dependency installation is blocked until
we choose a bootstrap path. That is the single gating decision (§9, Q1).

---

## 1. Architecture and component boundaries

Single-user, local-first, one machine. One backend process owns SQLite; the
dashboard is a separate dev server talking to it over localhost HTTP.

```
┌────────────────────────────┐        ┌──────────────────────────────┐
│ Next.js dashboard (3000)   │  HTTP  │ FastAPI backend (127.0.0.1:8765)
│ React + TS + Tailwind      │───────▶│  API layer, auth token check │
└────────────────────────────┘        └───────────────┬──────────────┘
                                                      │
                        ┌─────────────────────────────┼───────────────────────┐
                        │                             │                       │
                 ┌──────▼──────┐              ┌───────▼──────┐        ┌───────▼──────┐
                 │ Task engine │              │ SQLite (WAL) │        │ Audit writer │
                 │ + scheduler │◀────────────▶│  gitbot.db   │◀──────▶│ (append-only)│
                 └──────┬──────┘              └──────────────┘        └──────────────┘
                        │
          ┌─────────────┼──────────────┬──────────────────┐
          │             │              │                  │
    ┌─────▼─────┐ ┌─────▼─────┐ ┌──────▼──────┐  ┌────────▼────────┐
    │ OpenCode  │ │ Git ops   │ │ gh CLI      │  │ Test runner     │
    │ adapter   │ │ adapter   │ │ adapter     │  │ (pytest/npm)    │
    │(subprocess│ │(subprocess│ │(subprocess  │  │ (allowlisted    │
    │  or SDK)  │ │  git)     │ │  gh api)    │  │  argv only)     │
    └───────────┘ └───────────┘ └─────────────┘  └─────────────────┘
```

Boundaries that matter:

- **All external side effects go through an adapter** (`opencode/`, `gitops/`,
  `github/`, `tasks/exec.py`). Adapters take an explicit `cwd`, an argv list, a
  timeout, and an `Approval` handle. No module calls `subprocess` directly.
- **The task engine never touches GitHub.** It produces proposals; a separate
  approval gate executes them.
- **SQLite is the only source of truth.** No in-memory-only state that matters
  across a crash.
- **The dashboard is a client, not a peer.** It cannot read `~/.config/gh` or
  the DB directly.

### Repository layout

```
GitBot/
├── AGENTS.md, README.md, .gitignore, .env.example, pyproject.toml
├── docs/PLAN.md, docs/ARCHITECTURE.md, docs/SECURITY.md
├── backend/gitbot/
│   ├── main.py                # uvicorn entrypoint
│   ├── config.py              # settings loader (TOML + env, no secrets in code)
│   ├── db.py                  # connection factory, WAL, migrations runner
│   ├── schema/0001_init.sql
│   ├── models.py              # typed dataclasses + enums (no ORM)
│   ├── api/app.py, api/deps.py, api/routes/*.py
│   ├── github/client.py, github/repos.py, github/contributions.py
│   ├── opencode/client.py, opencode/events.py
│   ├── gitops/repo.py, gitops/diff.py
│   ├── tasks/engine.py, tasks/queue.py, tasks/policies.py, tasks/exec.py, tasks/scheduler.py
│   ├── security/allowlist.py, security/approvals.py, security/redact.py
│   └── audit.py
├── frontend/                  # Next.js App Router
├── tests/unit, tests/integration, tests/fixtures/fake_gh, tests/fixtures/fake_opencode
└── scripts/dev.sh             # documented, no secrets
```

---

## 2. How the pieces interact

| Component | Role | Talks to | Never does |
|---|---|---|---|
| FastAPI | Validation, auth, orchestration | engine, DB | spawn OpenCode directly |
| Task engine | State machine, retries, cancellation | adapters, DB, audit | push, merge, delete |
| SQLite | history, approvals, audit, schedules | engine, API | store tokens |
| `gh` CLI | all GitHub reads; gated writes | GitHub API | get its token read by us |
| `git` CLI | status/diff/branch/commit locally | working trees | rewrite history |
| OpenCode | the actual code reasoning/editing | target repo cwd | run outside allowlist |
| Next.js | dashboard UI | backend HTTP | touch the filesystem |

Data flow for one task:

```
POST /api/tasks → row(state=queued) → worker claims → preflight
  (allowlist, clean tree, dry_run) → OpenCode run → capture diff + event log
  → optional test run → row(state=awaiting_review, diff_hash) → UI
  → POST /api/tasks/{id}/approve (single-use token, diff_hash bound)
  → git commit (local only) → row(state=committed)
  → separate explicit approval required for push / PR
```

---

## 3. OpenCode integration (supported interfaces only)

**Tier 1 — CLI subprocess (MVP).**
`opencode run --format json --session <sid> --model <m> "<prompt>"` with
`cwd=<approved repo>`. Justification: it is a documented, stable, non-interactive
surface that returns machine-readable output and a session id we can persist for
continuation. One `opencode` process per task, own process group, stdout/stderr
captured, killed with SIGTERM→SIGKILL on timeout or user cancel.

Hard rules:
- `--auto` is **never** passed.
- Do **not** read or parse `~/.local/share/opencode` internal storage.
- Do **not** scrape the TUI.
- Repository-scoped `opencode.json` gets restrictive `permission` rules so a
  non-interactive run cannot silently execute `bash` or edit outside the repo.
  *(Phase 1 spike: confirm exactly how `run` reports a denied permission; the
  design must degrade to "task fails with a clear reason", not auto-approve.)*
- Prompt templates are ours, never assembled from repo content.

**Tier 2 — `opencode serve` + `@opencode-ai/sdk` (post-MVP).**
Only if we need live event streaming in the dashboard or a persistent server.
Bind to `127.0.0.1`, random port, ephemeral; no CORS wildcard. Prototype behind
a feature flag; keep Tier 1 as the fallback path.

Explicitly out of scope: reverse-engineering internal APIs, depending on
undocumented endpoints, or forking OpenCode.

---

## 4. GitHub auth and repository permissions

- **Auth:** reuse the existing `gh` keyring login. GitBot never reads
  `~/.config/gh/hosts.yml`, never calls `auth export`, never logs a token. All
  GitHub access is `gh api ...` / `gh repo list` via subprocess with argv lists.
- **Verification at startup:** `gh auth status` → record login name + scopes in
  the DB (metadata only). If scopes are missing for a requested feature, show
  the exact missing scope and why, and stop. No silent re-auth.
- **Scopes today** (`gist, read:org, repo, workflow`) are already broad; we will
  not request more. Contribution diagnostics need only GraphQL `viewer`.
- **Allowlist:** a repo becomes usable only after the user adds its absolute path
  *and* confirms the GitHub remote. Stored in `repos` with
  `path`, `remote_slug`, `default_branch`, `allow_commit`, `allow_push` (push
  default false). Every adapter call re-checks the path is inside an allowlisted
  root, resolving symlinks, and refuses `..` escapes.
- **Writes:** `push`, `pr create`, `repo delete`, `settings edit`, `merge` are
  each separate action types, each requiring its own approval containing the
  exact command and target. Default `dry_run = true`.
- **Rate limits:** read `X-RateLimit-Remaining`; back off; cache repo lists for
  a short TTL.

---

## 5. Secure task execution and approval

State machine:

```
queued → running → awaiting_review → approved → committed → (push_requested → pushed)
                  ↘ failed / cancelled / expired
```

Controls:

1. **Allowlist check** before spawn (realpath, must be a git repo, must not be
   `~/GitBot` itself unless explicitly allowed).
2. **Clean-tree preflight** — refuse to start on a dirty tree unless the user
   picks "work in a scratch branch"; GitBot always works on a dedicated branch
   `gitbot/<task-id>` so `main`/`master` is never modified in place.
3. **Approval records** are single-use, expire (default 30 min), bind the exact
   `diff_hash` + argv + repo, and are written to the audit log with the actor.
   Changing the diff invalidates the approval.
4. **Command templates only.** `tasks/exec.py` exposes a fixed registry
   (`pytest -q`, `npm test --silent`, `ruff check`, `git diff`, ...). Repository
   content cannot inject a command. Anything not in the registry requires an
   approval that displays the full argv.
5. **Process hygiene:** no `shell=True`, no `eval`, fixed `cwd`, scrubbed env
   (`GITHUB_TOKEN`, `GH_TOKEN`, `OPENCODE_*` removed except what a child needs),
   `PATH` pinned, wall-clock timeout, output cap (e.g. 2 MB) with truncation
   notice, process-group kill.
6. **Redaction:** `security/redact.py` scrubs `gho_`, `ghp_`, `github_pat_`,
   `-----BEGIN * PRIVATE KEY-----`, `AKIA…`, and `Authorization:` values from
   every log line, DB event row, and API response. Unit-tested with fixtures.
7. **Local API auth:** random token in `~/.config/gitbot/token` (0600), required
   header on every non-GET route; server binds `127.0.0.1` only.
8. **Dry-run mode** is the default for all mutating actions and can only be
   disabled per-action, with an audit entry.

---

## 6. Contribution tracking and legitimate diagnostics

Read-only, GraphQL `contributionsCollection` (verified working: 49, 0 restricted)
plus per-repo commit queries. Cached in `github_cache` with timestamps.

Diagnostics are **informational**, never a promise of squares:

- Uncommitted/unpushed commits in allowlisted repos.
- Commits on non-default branches (not counted).
- Author email/name mismatch vs. the GitHub account's verified emails → the most
  common real cause of "my commits don't count".
- Repo not owned/forked by the account; fork deleted or detached.
- Private contribution visibility setting (`restrictedContributionsCount = 0` here).
- Scheduled/manual commits that genuinely represent work are fine; GitBot will
  refuse any "backdate to fill the graph" workflow, and the UI says so plainly.

Explicit non-goals: fake commits, date rewriting, bot-like cadence, generating
issues/PRs for activity.

---

## 7. Dashboard pages and API

Pages (Next.js App Router, localhost only):

1. **Overview** — auth status, allowlisted repos, queue depth, last failures.
2. **Repositories** — discoverable repos from `gh`, add/remove allowlist, per-repo flags.
3. **Tasks** — list + filters; **Task detail** with event timeline, diff viewer,
   test output, approve/reject/cancel, retry.
4. **New Task** — type (analyze/bugfix/feature/docs/deps/tests), repo, prompt, model.
5. **Approvals** — pending gated actions with exact argv and diff hash.
6. **Activity** — contribution calendar + diagnostics list.
7. **Settings** — dry-run, timeouts, model, worker count, token rotation.
8. **Audit** — append-only log, filterable, exportable (redacted).

API (all JSON, `Authorization: Bearer <local-token>` for mutations):

```
GET  /api/health
GET  /api/github/status                 login, scopes, rate limit
GET  /api/github/repos                  discoverable repos
GET  /api/repos                         allowlist
POST /api/repos                         add (path, slug)   [approval-gated]
DELETE /api/repos/{id}                                     [approval-gated]
GET  /api/tasks?state=&repo=
POST /api/tasks                         create
GET  /api/tasks/{id}                    task + events + diff
POST /api/tasks/{id}/cancel
POST /api/tasks/{id}/retry
POST /api/tasks/{id}/approve            {approval_id, diff_hash}
POST /api/tasks/{id}/reject
GET  /api/approvals, POST /api/approvals/{id}/deny
GET  /api/activity/contributions
GET  /api/activity/diagnostics
GET  /api/audit?limit=&before=
GET/PUT /api/settings
```

---

## 8. Testing, logging, error handling, recovery

- **Unit** (`pytest`): state machine transitions, allowlist/symlink escape,
  approval expiry + diff-hash binding, redaction, DB migrations, argv builder.
- **Integration:** real temp git repos; **fake `gh` and fake `opencode`**
  executables first on `PATH` that record argv and emit canned JSON — so no
  network, no token, no model cost. FastAPI via `httpx` `TestClient`.
- **Smoke:** one opt-in test against the real `opencode` binary in a throwaway
  repo (`@pytest.mark.slow`), never against real GitHub.
- **Fixtures:** no real tokens/keys; synthetic strings that match redaction
  patterns.
- **Logging:** stdlib `logging` with a JSON formatter to
  `~/.local/state/gitbot/gitbot.log` (rotating) + DB `task_events`. Every line
  passes the redactor. Request ids correlate API → task → subprocess.
- **Errors:** typed exception hierarchy (`GitBotError` → `AllowlistError`,
  `ApprovalRequired`, `UpstreamError`, `TimeoutError`, `PolicyViolation`);
  mapped to stable API codes. No stack traces returned to the client.
- **Recovery:** on startup, tasks stuck in `running` are marked
  `failed(reason=interrupted)` and their worktrees/branches reported for cleanup
  — never auto-resumed, since a half-applied edit is untrustworthy. Retries are
  explicit and create a new task row linked to the original. SQLite in WAL mode
  with a single writer thread; migrations are forward-only, numbered, and
  idempotent.

---

## 9. Dependencies, versions, setup

**Recommended Python set (all verified cp314-compatible):**
`fastapi`, `uvicorn[standard]`→ prefer plain `uvicorn` (avoid uvloop/httptools
build risk on 3.14), `pydantic` v2, `pytest`, `httpx` (test client), `ruff`.
Deliberately **not** used: SQLAlchemy, Alembic, Celery, Redis, APScheduler,
typer, requests. Rationale: stdlib `sqlite3` + SQL, threads, and `argparse` cover
our needs and cut 3.14 risk.

**Frontend:** `next@15`, `react@19`, `typescript@5`, `tailwindcss@4`,
`@tanstack/react-query`, `zod`. Installed via npm 10.9.9.

**Bootstrap decision required (Q1).** `pip` and `ensurepip` are absent and sudo
is unavailable. Options:

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. uv (recommended)** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` → `~/.local/bin/uv` | No sudo, fast, manages its own Python + venv, lockfile | One third-party installer script (inspect before running) |
| B. venv + get-pip | `python3 -m venv --without-pip .venv` then `get-pip.py` | Verified working pieces | Two-step, manual, less reproducible |
| C. apt | `sudo apt install python3-pip python3-venv` | Cleanest system integration | **Blocked**: sudo needs a password |
| D. stdlib only | Write without FastAPI | Zero installs | Reinvents routing/validation; slower, worse |

**Setup commands (for approval, not yet run):**

```bash
# A
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.14 .venv
uv pip install -e ".[dev]"

# frontend
cd frontend && npm install
```

---

## 10. Phased roadmap with acceptance tests

**Phase 0 — Bootstrap (0.5 d).** uv + venv + pyproject + ruff + pytest; `git init`
commit; `.gitignore`, `.env.example` (empty placeholders only). Local branch → `main`.
*Accept:* `uv run pytest` collects 0 tests and exits 0; `ruff check` clean;
`python -c "import gitbot"` works; no secret strings in `git grep`.

**Phase 1 — Read-only spine (1–2 d).** config, SQLite schema + migration runner,
audit, redaction, `gh` adapter, `GET /api/health|github/status|github/repos|repos`.
*Accept:* redaction tests pass; fake-`gh` test returns 3 repos; DB created in a
temp dir; startup with a revoked login yields a clean error, not a traceback.

**Phase 2 — Allowlist + dry-run git (1–2 d).** add/remove repos, symlink-escape
tests, branch creation, diff capture, clean-tree preflight.
*Accept:* `../../etc` and symlink escapes rejected with tests; diff on a temp repo
matches `git diff`; no write outside allowlist.

**Phase 3 — Task engine + OpenCode Tier 1 (3–4 d).** queue, worker, state machine,
`opencode run` adapter, cancel/timeout, event stream, approvals.
*Accept:* end-to-end task on a temp repo with fake opencode reaches
`awaiting_review`; cancel kills the process group within 5 s; a diff modified
after approval is rejected; `--auto` never appears in captured argv.

**Phase 4 — Dashboard MVP (3–4 d).** Overview, Repositories, Tasks list/detail,
New Task, Approvals. Localhost bind, token auth.
*Accept:* a task can be created and approved entirely from the UI against the fake
backend; no page renders a token; `curl` without the header gets 401 on mutations.

**Phase 5 — Contribution tracking (1–2 d).** GraphQL pull, cache, diagnostics,
Activity page.
*Accept:* fixture-based tests for each diagnostic rule; live read returns the 49
figure without writing anything; no backdating code path exists.

**Phase 6 — Scheduler + hardening (2–3 d).** cron-ish schedules in SQLite,
recovery on restart, log rotation, docs, `docs/SECURITY.md`.
*Accept:* kill -9 mid-task → next start marks it interrupted and does not resume;
a monthly schedule fires once and records an audit row.

**MVP cut line = Phases 0–4.** A user can pick a repo, run a real OpenCode task in
it, see the diff and tests, approve a local commit. Nothing touches GitHub writes.
Phases 5–6 are follow-ups.

---

## Open questions for you

1. **Bootstrap:** approve Option A (uv) or prefer B (venv + get-pip)?
2. **Target repo for the first real task:** `~/whatsapp-message-sender`? It is the
   only real local repo, and GitBot's rules require explicit allowlisting.
3. **Default branch:** rename `master` → `main` before the first commit?
4. **Model selection** for OpenCode tasks: pin one (e.g. the free tier currently
   configured) or expose a dropdown in Settings from day one?
5. **Commit identity:** use the existing global git identity, or a dedicated
   per-task `gitbot` identity (clearer audit trail, but may not count as your
   contribution)?
