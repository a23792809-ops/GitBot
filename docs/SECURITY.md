# Security Model

GitBot is a single-user local tool. It reduces risk with an allowlist, a
disposable Bubblewrap sandbox, single-use diff-bound approvals, dry-run, and a
loopback-only API. It is not a multi-tenant service and has not been hardened for
untrusted network exposure.

## Trust boundaries

| Boundary | Control |
| --- | --- |
| Network | Server binds to loopback only; non-loopback values are rejected. |
| API reads and writes | Bearer token, constant-time comparison. Only `GET /api/health` is unauthenticated; repository paths, prompts, outputs, diffs, approvals, audit rows, git status/diff, and GitHub account data all require it. |
| Repository access | Explicit allowlist; absolute paths under `$HOME`; GitBot's own source tree refused. |
| Model execution | Repository Bubblewrap sandbox with no network or host credentials; optional authenticated host gateway over one Unix socket. |
| Host model runner | Separate Bubblewrap sandbox with no repository, no host home/credentials, and only `*.opencode.ai:443`. |
| Live repository | Never mounted into the sandbox; changes only via an approved patch. |
| Commit | `allow_commit`, non-dry-run, exact stored hash, unchanged branch/HEAD/tree, passing tests. |
| GitHub | Read-only `gh` calls; no push, PR, merge, delete, or settings write. |

## Repository allowlist

- No repository is touched until the user explicitly adds it.
- Paths are resolved and symlinks followed before checks; a link cannot escape
  the allowed root.
- Paths must be absolute, exist, be directories, and contain `.git`.
- A worktree/submodule `.git` file is accepted only when its `gitdir:` target
  exists, is a directory, and remains inside the allowed roots. A symlinked
  `.git` or a target outside the allowlist is rejected.
- Repositories whose attributes select a Git filter driver are refused. Git
  would otherwise execute repository-controlled clean/smudge/process commands
  during `status`, `diff`, or `apply`.
- Paths must be under `$HOME` and are rejected if they are GitBot's source tree
  or inside it.
- Stored paths are revalidated on every task run and Git API request. If an entry
  disappears or becomes unsafe, the request fails closed.
- Removing a repository is refused while task history references it. Make it
  read-only instead.

## Sandbox

`gitbot.security.sandbox` runs OpenCode with an argument list, never a shell. It:

- creates a disposable `git archive HEAD` snapshot;
- mounts the analyze/explain snapshot read-only at `/repo`;
- copies change tasks into a writable `/work/repo` with no `.git`;
- unshares user, PID, network, IPC, UTS, and cgroup namespaces;
- drops capabilities and disables nested user namespaces;
- clears the environment and withholds host credentials;
- exposes only runtime roots needed by OpenCode;
- enforces a hard timeout, cancellation watchdog, process-group SIGTERM/SIGKILL,
  and bounded output.

The generated OpenCode policy is verified with `opencode debug config` inside the
sandbox before the task runs. If Bubblewrap or policy verification is unavailable,
the task fails; there is no live-repository fallback.

Extras are validated at both ends. Mount targets must be one of the fixed
sandbox roots (`/run/gitbot`, `/etc/ssl`, `/etc/ca-certificates`, the specific
TLS/system files under `/etc`). Read-only sources must be the relay script GitBot
ships, those fixed TLS files/directories, or an executable mounted under
`/run/gitbot/bin`. Writable extra mounts must be Unix sockets named `*.sock`
inside a GitBot-created (`gitbot-*`) directory that is owned by the current user
and not group/world accessible. Home directories, repositories, credential
stores, and arbitrary host paths are rejected, and a directory can never be
mounted writable.

Snapshot Git commands also run with command-line configuration that overrides a
repository's `core.fsmonitor` and `core.hooksPath`, a global config of
`/dev/null`, no system config, and no optional locks. A malicious local Git
config therefore cannot become an executed command while GitBot prepares a
snapshot.

## AI gateway and host runner

- The repository sandbox has no direct network. It reaches the host only through
  a loopback relay bound to a single bind-mounted Unix socket.
- The relay is a stdlib byte pump; it does not parse HTTP, interpolate a shell,
  or choose the child command. The command is a fixed argv built by GitBot.
- Every gateway request must present the per-run capability token. Tokens are
  compared in constant time, never written to audit rows or logs, and hidden
  from `GatewayPolicy` repr output.
- Requests are bounded by body size, message count, message/tool text, prompt
  size, and output size. Only OpenAI-compatible chat completions and model
  listing are accepted.
- The host runner is a second Bubblewrap sandbox. It sees a private temporary
  tree, not the repository or the host home. Its only network path is an HTTP
  `CONNECT` tunnel that accepts `opencode.ai` and `*.opencode.ai` on port 443.
- Provider errors are redacted before they are returned to the sandbox.
- There is no unsandboxed fallback if either sandbox is unavailable.

The review baseline is a host-only bare Git repository outside the sandbox mount,
so a model cannot rewrite refs or the index to hide a change. Symlink and
submodule changes are rejected. Diffs over the review limit fail the task instead
of being silently truncated.

## Live Git execution

- Git commands receive a filtered environment; `GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_INDEX_FILE`, and similar redirection/injection variables are not inherited.
- Command-line configuration disables repository hooks, `core.fsmonitor`, GPG
  signing, pager execution, and automatic maintenance. Diffs disable external
  diff and textconv commands.
- A reviewed patch that writes `.git` metadata or adds, modifies, or renames a
  `.gitattributes` file is rejected before it is applied.
- Filter-driver repositories are rejected rather than run; this includes
  repositories that rely on repository-level Git LFS filters.
- Commit applies the exact stored patch, re-hashes the applied diff, and reverses
  its own patch if apply or commit fails.

## Test execution

The optional `test_command` is chosen by the user in repository settings. It is
never taken from repository content or model output. GitBot:

- requires a single line of at most 500 characters;
- splits it with `shlex` and rejects shell metacharacters;
- runs the fixed argv without a shell inside the same disposable sandbox;
- caps and redacts output;
- records `tests_status`, `test_exit_code`, command, and truncated output;
- blocks commit when the status is `failed` or `error`.

This is not a general shell. Do not configure a command that reads credentials or
talks to the network; the sandbox has no network route anyway.

## Approvals and commits

- Approvals are single-use, expire after 30 minutes, and bind the task,
  repository, action, and diff hash.
- Approval checks the stored hash, the live branch, `HEAD`, and a clean working
  tree.
- The pending list excludes unused-but-expired approvals and approvals whose task
  is no longer awaiting review.
- Commit re-verifies all of that, journals `task.commit_started`, applies the
  exact stored patch to the index, re-hashes the applied diff, consumes the
  approval only after the exact reviewed diff is staged, and commits locally.
- If commit fails, the approval is revoked, GitBot attempts to reverse its own
  patch so the previously clean working tree is restored, and the failure is
  reported explicitly.
- If the process dies during commit, startup recovery fails the task and revokes
  the approval instead of leaving a reusable approval or an invisible
  half-applied commit.
- Nothing is pushed, and no pull request, merge, or repository setting is
  changed.

## Tokens and secrets

- The API token is created with `0600` permissions and never printed by the CLI.
- The dashboard keeps the token in memory only and clears it on reload/unload.
- `GITBOT_LOG_LEVEL` is applied to the root logger, and every root handler gets a
  redaction filter. The fully formatted record is redacted, so a credential
  passed only through `%s` arguments is removed too.
- Logs, API responses, `.env.example`, and test fixtures must not contain tokens,
  passwords, or private keys.
- GitHub credentials stay in the existing `gh` login. GitBot never asks for a
  GitHub token of its own.
- Do not read `~/.ssh` private keys or unrelated sensitive files.

## Data

- SQLite stores task history, events, approvals, audit records, and repository
  permissions. Data/config directories are created with `0700`.
- Migrations are forward-only. Never edit an applied migration; add a new
  numbered file.
- Back up before applying migrations to an existing profile. See
  [`BACKUP.md`](BACKUP.md).

## Dry-run

`GITBOT_DRY_RUN=true` is the default and is enforced in the engine:

- write-task execution returns HTTP 403 and leaves the task queued;
- commits return HTTP 403;
- analyze/explain are read-only and still allowed;
- the scheduler skips write tasks and records the skip.

Keep dry-run on unless you are supervising a disposable repository.

## Known gaps

- The gateway and host runner are tested with fake runners; a live provider call
  is not part of the automated suite. Do not weaken either sandbox or copy
  credentials into `/work` to make a live call work.
- The host model runner sandbox has no provider credential store, so a live
  provider call currently fails closed. Supplying credentials is an explicit user
  decision and must not be done by injecting secrets into argv, environment
  variables, `/work`, or repository snapshots.
- The real profile has migrations `0001`–`0007` applied (2026-11-15, after a
  backup). Any *new* migration file still requires a backup and explicit
  approval; the startup path migrates unconditionally, so review new files
  before starting against the real profile.
- Cross-process cancellation is honoured: a second process sharing the SQLite
  database records a durable `task.cancel_requested` event, and the owning
  process's watchdog observes it within ~1 s and SIGTERMs the sandbox. On
  startup, stale `running` rows are marked `failed`; in-process cancellation is
  checked after model completion and after tests, so a late cancel cannot
  silently move a task into review.
- Git filter drivers are unsupported; enabling one makes the repository
  ineligible for GitBot operations.
- The scheduler is process-local and interval-based.
- The bearer token is a local single-user secret, not a multi-user auth system.
- The API is intended for loopback only. Do not expose it through a proxy or
  reverse tunnel.

## Reporting a security issue

Do not open a public issue containing exploit details or secrets. Reproduce in a
throwaway repository and database, collect only redacted logs, and describe the
impact and the smallest safe fix.
