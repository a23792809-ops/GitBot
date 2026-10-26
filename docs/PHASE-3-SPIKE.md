# Phase 3 Spike — OpenCode Non-Interactive Behaviour

**Status:** complete; findings only. No task engine, dashboard, commit, push,
pull-request, or merge operation was implemented. Awaiting review before Phase 3
implementation.

**Date:** 2026-10-27

**Environment:**

- OpenCode `v2.0.27`
- Default model observed in session metadata: `opencode/flint-40b-free`,
  agent `build` (no model was pinned during the spike)
- Python `3.14.4`, Git `2.53.0`, Linux
- No global `~/.config/opencode/opencode.json` or `.jsonc` existed, and no
  project OpenCode config existed in `~/GitBot`

## At a glance

| Outcome | How it was reported |
| ------- | ------------------- |
| Success | Exit `0`; final `text` event; empty stderr; no terminal "done" event. Session outcome `succeeded`. |
| Permission denial | Exit `0` and session outcome `succeeded` in every case. Whole-tool deny produced **no event**; resource deny produced `tool_use` with `status=error`; `ask` auto-rejected with stderr notices and retries. |
| Timeout | No OpenCode timeout signal. Wrapper kill gave exit `124`, partial NDJSON, an in-flight tool state only in session export, and a shutdown transport `error` event. |
| Process failure | Exit `1`; either one JSON `error` event (e.g. `provider.no-route`) or non-JSON usage text on stdout plus an error on stderr. |

## 1. Scope and safety

The approved spike covered only the documented non-interactive interface:
`opencode run --format json`. The `--auto` flag was never used. All experiments
ran in separate throwaway Git repositories under
`/tmp/opencode/phase3-spike/`, each with its own local `opencode.json` and no
remotes. No `gh` command, GitHub API call, network Git operation, push, pull
request, or merge was performed; the only commits were the local setup commits
described below.

Prompts asked only for harmless actions: `git status --short`, `echo`, and
`sleep`. The repositories were initialised and committed locally with a fake
identity solely to give OpenCode a realistic working tree.

`--standalone` was used for every measured run so a private server was used
instead of the user's shared background server. This reduces interference; it
is **not** a security sandbox. It still reads the user's OpenCode auth, data,
and configuration directories.

## 2. Method

For each scenario:

1. Create an isolated temp Git repository with one committed `README.md`.
2. Write a project `opencode.json` using the V2 `permissions` array
   (`action`, `resource`, `effect`; last matching rule wins). V2 uses
   `shell`, not the V1 `bash` name.
3. Run, with stdout/stderr/exit code captured separately:

   ```bash
   cd <temp-repo>
   timeout -s TERM -k 5 <N> \
     opencode run --standalone --format json --title <name> "<prompt>"
   ```

4. Parse every stdout line as NDJSON and summarise event and tool states.
5. For post-hoc state, use `opencode session export <session> --sanitize` to
   inspect `info.outcome`, assistant `finish`/`rawFinish`, model, and tokens.
   This is a read-only local operation.

No timeout, denial, or crash result was inferred from exit code alone; each was
checked against the event stream and, where applicable, session metadata.

## 3. Observed NDJSON interface

Each stdout line is a complete JSON object. No non-JSON lines were observed on
successful model runs. Top-level keys are `type`, `timestamp` (epoch
milliseconds), `sessionID`, and either `part` or (for fatal errors) `error`.

Event types observed:

| `type`        | Payload of interest                                                        |
| ------------- | -------------------------------------------------------------------------- |
| `step_start`  | `part.type = "step-start"`; `snapshot`                                     |
| `tool_use`    | `part.tool` (`shell`, `execute`), `part.state.status`, input/output/error  |
| `step_finish` | `part.reason` (e.g. `tool-calls`), `tokens`, `cost`, `snapshot`            |
| `text`        | `part.text` (assistant text, including the final answer)                   |
| `error`       | `error.type`, `error.message` (top-level failure, e.g. provider or transport) |

A completed shell call looked like:

```json
{"type":"tool_use","sessionID":"...","timestamp":...,"part":{"type":"tool","tool":"shell","state":{"status":"completed","input":{"command":"git status --short"},"output":"?? opencode.json\n"}}}
{"type":"step_finish","sessionID":"...","timestamp":...,"part":{"type":"step-finish","reason":"tool-calls","tokens":{...},"cost":0}}
```

A denied resource looked like:

```json
{"type":"tool_use","sessionID":"...","timestamp":...,"part":{"type":"tool","tool":"shell","state":{"status":"error","input":{"command":"git status --short","workdir":"..."},"error":"Permission denied: shell"}}}
```

Notes:

- `part.state.error` was a **string** in every observed tool error; a top-level
  `error` is an **object** with `type` and `message`.
- `tool_use` is emitted only when the tool call reaches a terminal state. There
  is no separate "tool started" event. While a long command runs, no tool event
  is produced.
- The final assistant text is not followed by a `step_finish` event in the
  observed runs. There is no explicit "done" event.
- `step_start` can repeat during a run. It is not proof of progress and is not
  a heartbeat contract.

## 4. Results

| Test | Permission setup | Observation | Exit |
| ---- | ---------------- | ----------- | ---- |
| baseline | no tools requested | `step_start` → `text` "pong"; 2 lines | 0 |
| allow-shell | deny all `shell`; allow `git status *` | `tool_use` completed, output `?? opencode.json`; final text; 5 lines | 0 |
| deny-shell | deny all `shell` | **No shell attempt/event.** Model used the `execute` (Code Mode) tool to search for a shell tool, found none, and reported it could not run the command; 9 lines | 0 |
| deny-resource | allow all `shell`; deny `git status *` | `tool_use` status `error`, error `"Permission denied: shell"`; final text (misleadingly begins "The command was run, but..."); 5 lines | 0 |
| ask-shell | deny all `shell`; `ask` for `git status *` | 4 stderr auto-reject notices; 6 `tool_use` error events over repeated retries; 20 lines | 0 |
| sleep5 | allow `sleep *`; deny other `shell` | Tool completed after ~5.6 s; final text "done" | 0 |
| sleep30 | same | Tool completed after ~32.1 s; final text "done" | 0 |
| timeout-137 | same, `sleep 137` | Killed at 20 s; see timeout section | 124 (GNU time-out) |
| timeout-90 | same, `sleep 90` | Killed at 30 s; same shutdown pattern | 124 (GNU time-out) |
| timeout-60 | same, `sleep 60` | 11 `step_start` events, no assistant message persisted, 0 tokens; killed at 90 s | 124 (GNU time-out) |
| invalid-model | none | One JSON `error` event, `provider.no-route`, "Model unavailable: ..."; stderr empty | 1 |
| invalid-flag | none | Human-readable help on **stdout**, error on stderr; no JSON | 1 |
| bad-json-config | malformed `opencode.json` | Config silently ignored; shell command executed under default allow | 0 |
| bad-effect-config | invalid `effect: "deny_typo"` | Config silently ignored; shell command executed under default allow | 0 |

### 4.1 Timeout details

OpenCode exposed no timeout event or timeout exit code in these runs. Every
observed timeout was imposed by the wrapper's `timeout` command:

- Exit code was `124` (GNU `timeout`), stderr was empty.
- NDJSON ended with `step_start` followed by a top-level `error` of type
  `unknown`: `"Transport: The socket connection was closed unexpectedly..."`.
  The event timing coincided with the wrapper's SIGTERM after allowing for
  process start-up, so this is a **shutdown artifact**, not evidence of an
  internal timeout.
- No terminal `step_finish` or final `text` event was present.
- No `tool_use` event described the in-flight command, because that event is
  only written after completion. The command that was running at kill time is
  therefore **not recoverable from the stdout stream**.
- Where an assistant/tool message had been persisted, session export showed
  `info.outcome: null`, assistant `finish: "error"`, `rawFinish: "tool_calls"`,
  and a tool part in `error` state (the 60-second retry-loop attempt had only
  the user message).
- No orphaned `sleep` or OpenCode process was found after the kill in the
  observed runs; the in-flight tool's process was not observed afterward.

The 60-second attempt did not produce an assistant message or any tool event;
its session export showed only the user message and zero tokens. The stream and
session metadata are consistent with a model/provider retry loop (11
`step_start` events) rather than a 60-second tool limit. A 30-second tool call
completed normally in a separate run.

## 5. Findings

### 5.1 Exit 0 does not mean the task succeeded

Every permission-denial scenario exited `0`, and session metadata marked it
`succeeded`:

- a fully denied tool disappeared from the catalogue and was never attempted;
- an auto-rejected `ask` request was retried several times and then explained;
- a resource-level denial produced a tool error and a final answer.

An adapter that equates exit `0` with success would report completed tasks that
did nothing. Success must be derived from the event stream **and** from
independent verification of the requested effect (diff, test result, or
required output).

### 5.2 Whole-tool `deny` is invisible

With `shell` fully denied, the tool was removed from the agent's catalogue. The
model did not attempt it and no denial event was emitted. It searched via the
`execute` (Code Mode) tool, found no shell tool, and produced a final answer
saying it could not run the command. Exit code and outcome were both "success".

For GitBot this means:

- Do not rely on the model reporting that a required tool was unavailable.
- Do not assume a denied tool will produce an error event that can be logged.
- If a task requires evidence of a tool action, require that evidence directly
  instead of inferring it from the transcript.
- `execute` (Code Mode) remained available when `shell` was denied. Its nested
  tools still enforce their own permission rules in the documented model, but
  the exact nested enforcement was not tested. GitBot should decide explicitly
  whether to deny `execute` for tasks that do not need it.

### 5.3 Resource-scoped `deny` is detectable

When the tool existed but one resource was denied, OpenCode emitted a
`tool_use` event with `state.status = "error"` and a permission message. This is
detectable, but the model's final text can be misleading (it said the command
"was run" before describing the permission failure). The adapter must inspect
tool states, not prose.

Implication: for actions GitBot can predict, resource-scoped rules produce
better audit evidence. Security must not depend on that evidence, because a
whole-tool deny produces none.

### 5.4 `ask` is not an approval channel under `run`

In a non-interactive run without `--auto`, an `ask` decision is automatically
rejected:

```text
! permission requested: shell (git status --short); auto-rejecting
```

The tool event carried:

```text
This non-interactive run cannot ask the user for permission, so the request was rejected. Continue without this action.
```

The model retried the same request several times (four stderr notices, six tool
errors) before giving up, without user interaction. Exit code remained `0`.

Therefore:

- `ask` cannot be used to obtain user approval in `run` mode.
- `--auto` must remain forbidden; it would convert the same decision into
  execution without review.
- GitBot approvals must be enforced **before** invoking OpenCode, using a
  pre-approved command/resource allowlist. The post-run diff approval remains a
  separate GitBot control.
- Denial retries should be capped by the adapter/watchdog; otherwise a small
  model can burn tokens repeating a rejected action.

### 5.5 Timeout ownership is external

OpenCode did not time out during these runs and exposed no timeout signal. The
wrapper must:

- own the wall-clock deadline;
- kill the **process group** (TERM, then KILL after a grace period);
- classify the result as `Timeout` even if a shutdown transport `error` event
  was emitted;
- record that the in-flight command is unknown from the stream;
- rely on session export only as supplementary post-mortem evidence.

A per-command timeout in GitBot's own exec adapter is a better fit for long
builds/tests than keeping a single OpenCode run alive for minutes. `run` did
successfully execute a 30-second tool call, but the 60-second attempt produced
a retry-loop-like stream, and longer calls were killed. Long-running reliability
of `run` was not established.

### 5.6 Process failures are not uniform

Observed startup failures:

- Unknown model: exit `1`; single JSON `error` event with
  `type: "provider.no-route"`; stderr empty.
- Unknown CLI flag: exit `1`; **help text on stdout**, usage error on stderr,
  no JSON at all.

Consequences:

- The parser must tolerate non-JSON stdout when the exit code is non-zero.
- A non-zero exit code is authoritative for process failure, but the error
  detail may be on stdout, stderr, or both.
- ANSI escape sequences appear in stderr (the auto-reject notices); logs must
  strip them before storage.
- In these runs, successful runs produced empty stderr. Do not assume stderr is
  empty on failure, and do not parse stderr as JSON.

### 5.7 Configuration fails open

A malformed `opencode.json` and a schema-invalid permission effect were both
**silently ignored**:

- `opencode debug config` exited `0` and listed only the global config
  directory, with no warning.
- A subsequent run executed `echo bad-config-open` successfully under default
  permissions.

A valid config file appeared in `debug config` as a `document` entry with its
parsed `info`. Therefore GitBot must:

1. Validate the JSON and the permission schema itself before running.
2. Run `opencode debug config` and assert that the expected project document
   and rules are present.
3. Abort the task if the config is missing, ignored, or supplemented by
   unexpected sources.

The default permission policy is broad: with no valid project config, `shell`
executed commands; in a valid config that denied `shell`, `execute` was still
available. Never run a task relying on OpenCode's defaults.

### 5.8 Session outcome cannot substitute for event inspection

Session exports contained:

| Scenario | `info.outcome` | Assistant finish |
| -------- | -------------- | ---------------- |
| baseline / allow | `succeeded` | `stop` |
| deny (tool removed) | `succeeded` | `stop` |
| ask auto-reject | `succeeded` | `stop` |
| invalid model | `failed` | (no assistant message) |
| wrapper timeout | `null` | `error` |

So session metadata is useful for audit, tokens, and model identity, but it
does **not** identify permission denials. It is not a success signal.

The session export also recorded the model as `opencode/flint-40b-free` and the
agent as `build`. GitBot should pin both rather than depend on a changing
default.

## 6. Implications for Phase 3 implementation

Recommended adapter/classifier rules, pending review:

1. **Invocation:** `opencode run --standalone --format json --model <pinned>`
   with a controlled `cwd`, filtered environment, no `--auto`, and a dedicated
   process group. Prompt/argv handling must avoid secrets; argv is visible to
   other processes of the same user.
2. **Parsing:** strict per-line JSON; cap line/event counts; treat non-JSON
   stdout as a protocol failure unless the process exited non-zero with usage
   text; strip ANSI from stderr.
3. **Classification precedence:** wrapper timeout → process exit non-zero →
   top-level `error` → permission-denied tool error → other tool error →
   completed candidate. Exit `0` alone is never success.
4. **Permission denial:** terminal state for the task. Do not auto-retry. Audit
   the denied action and surface it to the user.
5. **Verification:** for tasks with effects, require an independent Git diff,
   test result, or required output. For read-only tasks, require the expected
   evidence, not only a final text block.
6. **Timeout:** own the deadline; kill the process group; classify timeout
   before interpreting shutdown transport errors; per-command timeouts for
   builds/tests.
7. **Config:** generate a minimal per-task `opencode.json`, validate it against
   the V2 schema, assert `opencode debug config` includes it, and fail closed on
   any mismatch. Check for unexpected ancestor/global config sources.
8. **Approvals:** pre-run, explicit, allowlisted; single-use and bound to the
   action (the planned `diff_hash` approval pattern remains for post-run
   changes). No reliance on `ask`.
9. **Retry policy:** bounded; never retry permission denials; distinguish
   provider retry loops (repeated `step_start` with no assistant progress).
10. **Audit:** optionally export the session after completion for model,
    tokens, and outcome; do not use outcome to override permission evidence.
11. **Tests:** hermetic NDJSON fixture tests for success, completed tool,
    resource denial, whole-tool denial (no event), ask auto-reject, startup
    error, non-JSON usage error, and timeout kill. A fake `opencode` executable
    keeps CI network-free.
12. **Deferred:** Tier 2 (`opencode serve` + SDK) may offer a better permission
    and cancellation channel, but it was not tested here and remains post-MVP.

## 7. Verification

No application code was changed for this spike; only this document was added.
The existing suite and lint were run:

- `uv run pytest -q`: **150 passed**, 1 warning (existing
  `StarletteDeprecationWarning` about `httpx`/`httpx2` in `TestClient`), 2.73 s.
- `uv run ruff check .`: **All checks passed!**
- `uv run ruff format --check .`: **52 files already formatted.**

## 8. Cleanup

- The throwaway repositories and captured outputs under
  `/tmp/opencode/phase3-spike/` were removed after this report was written.
- OpenCode stored session metadata for the spike under its local data
  directory (titles such as `gitbot-spike-*`, plus one untitled session from
  the invalid-model test). That is user-level OpenCode state rather than a
  temporary file, so it was not deleted automatically. It can be removed with
  `opencode session delete <session-id>` after review.
- No temporary files outside `~/GitBot` remain from the experiment; the
  OpenCode session metadata noted above is the only residual user-level state.
- No remote GitHub or Git operation was performed.

## 9. Limitations

- One OpenCode version, one default model, one machine.
- Small sample; model/provider behaviour was not deterministic. A 60-second
  run entered a retry loop while a 30-second run succeeded.
- No network isolation, container, or separate OS user; `--standalone` is not
  a sandbox.
- Only `shell` permission paths were exercised; `edit`, `write`, and nested
  Code Mode enforcement were not tested.
- No interactive TUI, `--auto` (forbidden), `serve`/SDK, cancellation API, or
  Windows/macOS testing.
- No long-duration build or test command was validated.
- The root cause of the provider retry loop was not investigated in OpenCode's
  internal logs; conclusions are based on observable CLI/session behaviour.

## 10. Recommended next step

Review these findings. If approved, implement Phase 3 in this order:

1. Adapter and NDJSON parser/classifier with hermetic fixture tests.
2. SQLite migration `0002` and the task state machine.
3. Per-task `gitbot/<task-id>` branch creation behind preflight and approval.
4. Diff-hash-bound, single-use approvals.
5. Only then connect the task engine to OpenCode, with pre-run allowlists,
   wrapper-owned timeouts, and post-run verification.
