"""OpenCode subprocess adapter with wrapper-owned timeouts and classification.

Implements Phase 3 spike rules: never trust exit 0 alone, own process group
kill, classify permission denials, handle non-JSON failures, cap output.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from gitbot.errors import PolicyViolation, UpstreamError
from gitbot.opencode.parser import OpenCodeEventParser
from gitbot.security import redact_text


class RunClassification(StrEnum):
    SUCCESS = "success"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    PROCESS_ERROR = "process_error"
    PROTOCOL_ERROR = "protocol_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OpenCodeConfig:
    executable: str = "opencode"
    model: str | None = None
    agent: str | None = None
    timeout_s: float = 300.0
    kill_grace_s: float = 5.0
    max_output_bytes: int = 2 * 1024 * 1024
    max_output_chars: int = 64 * 1024
    env: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class OpenCodeResult:
    classification: RunClassification
    exit_code: int | None
    events: list
    session_id: str | None
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    error_message: str | None = None
    denied_actions: list[dict] | None = None
    output: str | None = None
    output_truncated: bool = False


def _filtered_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    # Scrub common token env vars
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "OPENCODE_API_KEY",
    ):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    return env


def _terminate_group(proc: subprocess.Popen[str], sig: int) -> None:
    """Best-effort signal to the child's process group."""
    if not proc.pid:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def _assistant_output(parser: OpenCodeEventParser, max_chars: int) -> tuple[str | None, bool]:
    """Redact and bound assistant text before it can be persisted or served."""
    text = parser.strip_ansi("\n".join(parser.text_parts)).strip()
    text = redact_text(text).strip()
    if not text:
        return None, False
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def build_run_argv(
    config: OpenCodeConfig, prompt: str, *, session_id: str | None = None
) -> list[str]:
    """Build the fixed, non-interactive ``opencode run`` argv.

    ``--auto`` is never emitted; callers must not append it either (the sandbox
    builder rejects it independently).
    """
    argv = [config.executable, "run", "--standalone", "--format", "json"]
    if config.model:
        argv.extend(["--model", config.model])
    if config.agent:
        argv.extend(["--agent", config.agent])
    if session_id:
        argv.extend(["--session", session_id])
    argv.append(prompt)
    return argv


def probe_opencode(
    config: OpenCodeConfig | None = None, *, timeout_s: float = 5.0
) -> dict[str, object]:
    """Report whether the OpenCode executable exists without calling a model."""
    effective = config or OpenCodeConfig()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [effective.executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_filtered_env(effective.env),
        )
    except FileNotFoundError:
        return {
            "available": False,
            "executable": effective.executable,
            "version": None,
            "detail": "opencode executable was not found on PATH",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "executable": effective.executable,
            "version": None,
            "detail": f"opencode version probe failed: {exc}",
        }
    output = redact_text((completed.stdout or completed.stderr or "").strip())
    return {
        "available": completed.returncode == 0,
        "executable": effective.executable,
        "version": output[:200] or None,
        "detail": None if completed.returncode == 0 else output[:500],
    }


@dataclass(frozen=True, slots=True)
class ClassifiedStream:
    """One classification decision for captured OpenCode stdout/stderr."""

    classification: RunClassification
    events: tuple
    session_id: str | None
    error_message: str | None = None
    denied_actions: list[dict] | None = None
    output: str | None = None
    output_truncated: bool = False


def classify_stream(
    stdout: str,
    stderr: str,
    *,
    exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
    max_output_chars: int,
    session_id: str | None = None,
) -> ClassifiedStream:
    """Classify one completed run. Exit code 0 alone is never treated as success.

    Shared by the direct subprocess adapter and the sandboxed analysis runner so
    both paths make the same permission/timeout/error decisions.
    """
    parser = OpenCodeEventParser()
    events = parser.parse_stream(stdout)
    session_id_found = next((e.session_id for e in events if e.session_id), session_id)
    output, output_truncated = _assistant_output(parser, max_output_chars)

    if cancelled:
        return ClassifiedStream(
            classification=RunClassification.CANCELLED,
            events=tuple(events),
            session_id=session_id_found,
            error_message="cancelled",
        )

    if timed_out:
        return ClassifiedStream(
            classification=RunClassification.TIMEOUT,
            events=tuple(events),
            session_id=session_id_found,
            error_message="timeout",
        )

    if parser.permission_denied or parser.denied_actions:
        return ClassifiedStream(
            classification=RunClassification.PERMISSION_DENIED,
            events=tuple(events),
            session_id=session_id_found,
            error_message="permission denied",
            denied_actions=parser.denied_actions,
        )

    if parser.top_level_error:
        return ClassifiedStream(
            classification=RunClassification.PROCESS_ERROR,
            events=tuple(events),
            session_id=session_id_found,
            error_message=str(parser.top_level_error.get("message", "")),
        )

    if exit_code != 0:
        redacted_stderr = redact_text(parser.strip_ansi(stderr))
        redacted_stdout = redact_text(parser.strip_ansi(stdout))
        message = redacted_stderr.strip() or redacted_stdout.strip() or f"exit code {exit_code}"
        return ClassifiedStream(
            classification=RunClassification.PROCESS_ERROR,
            events=tuple(events),
            session_id=session_id_found,
            error_message=message[:500],
        )

    if parser.has_final_text or any(e.type.value == "step_finish" for e in events):
        return ClassifiedStream(
            classification=RunClassification.SUCCESS,
            events=tuple(events),
            session_id=session_id_found,
            output=output,
            output_truncated=output_truncated,
        )

    if events:
        return ClassifiedStream(
            classification=RunClassification.SUCCESS,
            events=tuple(events),
            session_id=session_id_found,
            output=output,
            output_truncated=output_truncated,
        )

    return ClassifiedStream(
        classification=RunClassification.PROTOCOL_ERROR,
        events=(),
        session_id=session_id_found,
        error_message="no events produced",
    )


def count_completed_tool_calls(events: Sequence[object]) -> int:
    """Count tool calls that reached a successful terminal state.

    A read-only task has repository evidence only when the sandboxed agent
    actually completed a tool call.  A stream with no completed tool call means
    the model answered from assumption, so the caller must fail closed.
    """
    total = 0
    for event in events:
        if getattr(event, "type", None) != "tool_use":
            continue
        part = getattr(event, "part", None)
        if not isinstance(part, dict):
            continue
        state = part.get("state")
        status = state.get("status") if isinstance(state, dict) else None
        if status in (None, "completed"):
            total += 1
    return total


class OpenCodeAdapter:
    def __init__(self, config: OpenCodeConfig | None = None) -> None:
        self.config = config or OpenCodeConfig()

    def _build_argv(self, prompt: str, *, session_id: str | None = None) -> list[str]:
        return build_run_argv(self.config, prompt, session_id=session_id)

    def run(
        self,
        prompt: str,
        *,
        cwd: Path | str,
        session_id: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        cancel_event: threading.Event | None = None,
        allow_unsandboxed: bool = False,
    ) -> OpenCodeResult:
        """Run OpenCode directly on the host (tests/tooling only).

        Task execution never uses this path: every analyze/explain/change lane
        runs OpenCode inside the repository Bubblewrap sandbox through the
        authenticated gateway.  Keeping a default-deny guard here means a future
        caller cannot accidentally reintroduce unsandboxed host execution; test
        harnesses that intentionally exercise the raw adapter must opt in.
        """
        if not allow_unsandboxed:
            raise PolicyViolation(
                "unsandboxed OpenCode execution is disabled; task lanes use the "
                "sandboxed gateway and an explicit opt-in is required for tests",
                details={"adapter": "OpenCodeAdapter.run"},
            )
        cwd_path = Path(cwd)
        if not cwd_path.exists():
            raise PolicyViolation(f"cwd does not exist: {cwd_path}")
        argv = self._build_argv(prompt, session_id=session_id)
        effective_timeout = timeout_s if timeout_s is not None else self.config.timeout_s
        run_env = _filtered_env(env if env is not None else self.config.env)

        started = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=str(cwd_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=run_env,
                start_new_session=True,  # create process group
                text=True,
            )
        except FileNotFoundError as exc:
            raise UpstreamError(
                f"{self.config.executable!r} was not found on PATH",
                details={"executable": self.config.executable},
            ) from exc

        stdout = ""
        stderr = ""
        stdout_truncated = False
        stderr_truncated = False
        timed_out = False
        cancelled = False

        watcher: threading.Thread | None = None
        stop_watcher = threading.Event()
        if cancel_event is not None:

            def _watch() -> None:
                nonlocal cancelled
                while not stop_watcher.wait(0.2):
                    if proc.poll() is not None:  # type: ignore[union-attr]
                        return
                    if not cancel_event.is_set():
                        continue
                    cancelled = True
                    _terminate_group(proc, signal.SIGTERM)  # type: ignore[arg-type]
                    deadline = time.monotonic() + self.config.kill_grace_s
                    while time.monotonic() < deadline and proc.poll() is None:  # type: ignore[union-attr]
                        stop_watcher.wait(0.05)
                    if proc.poll() is None:  # type: ignore[union-attr]
                        _terminate_group(proc, signal.SIGKILL)  # type: ignore[arg-type]
                    return

            watcher = threading.Thread(target=_watch, name="gitbot-cancel", daemon=True)
            watcher.start()

        try:
            stdout, stderr = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill process group
            try:
                if proc.pid:
                    os.killpg(proc.pid, signal.SIGTERM)
            except Exception:  # noqa: S110
                pass
            try:
                stdout, stderr = proc.communicate(timeout=self.config.kill_grace_s)
            except subprocess.TimeoutExpired:
                try:
                    if proc.pid:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:  # noqa: S110
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=2.0)
                except Exception:
                    stdout, stderr = stdout or "", stderr or ""
            except Exception:  # noqa: S110
                pass
        except KeyboardInterrupt:
            cancelled = True
            try:
                if proc.pid:
                    os.killpg(proc.pid, signal.SIGTERM)
            except Exception:  # noqa: S110
                pass
            try:
                proc.communicate(timeout=self.config.kill_grace_s)
            except Exception:
                try:
                    if proc.pid:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:  # noqa: S110
                    pass

        duration_ms = int((time.monotonic() - started) * 1000)

        # Truncate if needed
        if len(stdout.encode("utf-8", errors="replace")) > self.config.max_output_bytes:
            # naive truncate by chars
            stdout = stdout[: self.config.max_output_bytes]
            stdout_truncated = True
        if len(stderr.encode("utf-8", errors="replace")) > self.config.max_output_bytes:
            stderr = stderr[: self.config.max_output_bytes]
            stderr_truncated = True

        if cancel_event is not None and cancel_event.is_set():
            cancelled = True

        classified = classify_stream(
            stdout,
            stderr,
            exit_code=proc.returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            max_output_chars=self.config.max_output_chars,
            session_id=session_id,
        )
        result_exit_code = proc.returncode
        if timed_out and proc.returncode != 0:
            result_exit_code = 124
        return OpenCodeResult(
            classification=classified.classification,
            exit_code=result_exit_code,
            events=list(classified.events),
            session_id=classified.session_id,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_ms=duration_ms,
            error_message=classified.error_message,
            denied_actions=classified.denied_actions,
            output=classified.output,
            output_truncated=classified.output_truncated,
        )
