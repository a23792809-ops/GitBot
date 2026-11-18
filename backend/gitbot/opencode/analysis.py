"""Read-only, sandboxed repository analysis.

The analysis lane is deliberately separate from the write lane:

* the repository is snapshotted with ``git archive HEAD`` and mounted read-only
  at ``/repo`` (the working tree and ``.git`` are never visible);
* the process runs in a bubblewrap sandbox with no external network and a
  cleared environment, so no host credential or provider session can leak in;
  when enabled, the only route out is a loopback relay to the authenticated
  host AI gateway;
* OpenCode is only allowed to ``read``; ``edit``, ``write``, ``shell`` and
  ``execute`` are denied;
* the policy is re-read by ``opencode debug config`` *inside* the sandbox and
  must match exactly, because OpenCode silently ignores malformed config;
* the run ends in the terminal ``analyzed`` state with a bounded, redacted text
  response -- never a diff, branch, approval, or commit.

There is intentionally no fallback that runs the model outside the sandbox.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gitbot.opencode.adapter import (
    OpenCodeConfig,
    RunClassification,
    build_run_argv,
    classify_stream,
    count_completed_tool_calls,
)
from gitbot.opencode.gateway import (
    GatewayConfig,
    ai_gateway_session,
    classify_provider_error,
)
from gitbot.opencode.policy import PermissionRule
from gitbot.opencode.sandbox_policy import install_policy, verify_policy
from gitbot.security.allowlist import PROJECT_ROOT
from gitbot.security.sandbox import isolated_repository, run_sandbox_command

ANALYSIS_RULES: tuple[PermissionRule, ...] = (
    PermissionRule("read", "*", "allow"),
    PermissionRule("edit", "*", "deny"),
    PermissionRule("write", "*", "deny"),
    PermissionRule("shell", "*", "deny"),
    PermissionRule("execute", "*", "deny"),
)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    classification: RunClassification
    snapshot_head: str
    duration_ms: int
    output: str | None = None
    output_truncated: bool = False
    session_id: str | None = None
    error_message: str | None = None
    denied_actions: list[dict] | None = None
    tool_calls: int = 0


def run_analysis(
    source: Path | str,
    prompt: str,
    *,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (PROJECT_ROOT,),
    config: OpenCodeConfig | None = None,
    timeout_s: float | None = None,
    cancel_event: threading.Event | None = None,
    temporary_root: Path | str | None = None,
    bwrap: str = "bwrap",
    require_clean: bool = True,
    gateway: GatewayConfig | None = None,
) -> AnalysisResult:
    """Run one read-only analysis and return a classified, redacted result."""
    effective_config = config or OpenCodeConfig()
    effective_timeout = effective_config.timeout_s if timeout_s is None else timeout_s

    with ai_gateway_session(gateway, bwrap=bwrap, cancel_event=cancel_event) as endpoint:
        command = build_run_argv(effective_config, prompt)
        ro_binds: tuple[tuple[Path, str], ...] = ()
        rw_binds: tuple[tuple[Path, str], ...] = ()
        gateway_policy = None
        if endpoint is not None:
            command, ro_binds = endpoint.prepare(
                command,
                executable=effective_config.executable,
            )
            rw_binds = endpoint.rw_binds
            gateway_policy = endpoint.policy

        with isolated_repository(
            source,
            allowed_roots=allowed_roots,
            forbidden_roots=forbidden_roots,
            command=command,
            extra_ro_binds=ro_binds,
            extra_rw_binds=rw_binds,
            temporary_root=temporary_root,
            bwrap=bwrap,
            require_clean=require_clean,
        ) as sandbox:
            install_policy(sandbox.snapshot.root, ANALYSIS_RULES, gateway=gateway_policy)
            verify_policy(
                sandbox.snapshot.root,
                sandbox.workspace.root,
                effective_config,
                rules=ANALYSIS_RULES,
                gateway=gateway_policy,
                bwrap=bwrap,
            )

            if cancel_event is not None and cancel_event.is_set():
                return AnalysisResult(
                    classification=RunClassification.CANCELLED,
                    snapshot_head=sandbox.snapshot.head,
                    duration_ms=0,
                    error_message="cancelled",
                )

            run = run_sandbox_command(
                sandbox.command,
                timeout_s=effective_timeout,
                cancel_event=cancel_event,
                kill_grace_s=effective_config.kill_grace_s,
            )
            classified = classify_stream(
                run.stdout,
                run.stderr,
                exit_code=run.exit_code,
                timed_out=run.timed_out,
                cancelled=run.cancelled,
                max_output_chars=effective_config.max_output_chars,
            )
            if classified.classification in (
                RunClassification.PROCESS_ERROR,
                RunClassification.PROTOCOL_ERROR,
            ):
                provider_error = classify_provider_error(classified.error_message or "")
                if provider_error is not None:
                    raise provider_error
            return AnalysisResult(
                classification=classified.classification,
                snapshot_head=sandbox.snapshot.head,
                duration_ms=run.duration_ms,
                output=classified.output,
                output_truncated=classified.output_truncated,
                session_id=classified.session_id,
                error_message=classified.error_message,
                denied_actions=classified.denied_actions,
                tool_calls=count_completed_tool_calls(classified.events),
            )
