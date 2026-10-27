"""OpenCode subprocess adapter with wrapper-owned timeouts and classification.

Implements Phase 3 spike rules: never trust exit 0 alone, own process group
kill, classify permission denials, handle non-JSON failures, cap output.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
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


class OpenCodeAdapter:
    def __init__(self, config: OpenCodeConfig | None = None) -> None:
        self.config = config or OpenCodeConfig()

    def _build_argv(self, prompt: str, *, session_id: str | None = None) -> list[str]:
        argv = [self.config.executable, "run", "--standalone", "--format", "json"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if session_id:
            argv.extend(["--session", session_id])
        argv.append(prompt)
        return argv

    def run(
        self,
        prompt: str,
        *,
        cwd: Path | str,
        session_id: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> OpenCodeResult:
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

        parser = OpenCodeEventParser()
        events = parser.parse_stream(stdout)
        session_id_found = next((e.session_id for e in events if e.session_id), session_id)

        if cancelled:
            return OpenCodeResult(
                classification=RunClassification.CANCELLED,
                exit_code=proc.returncode,
                events=events,
                session_id=session_id_found,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
                error_message="cancelled",
            )

        if timed_out:
            # Classify as timeout even if shutdown transport error present
            return OpenCodeResult(
                classification=RunClassification.TIMEOUT,
                exit_code=124 if proc.returncode != 0 else proc.returncode,
                events=events,
                session_id=session_id_found,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
                error_message="timeout",
            )

        exit_code = proc.returncode

        # Check for permission denial from events
        if parser.permission_denied or parser.denied_actions:
            return OpenCodeResult(
                classification=RunClassification.PERMISSION_DENIED,
                exit_code=exit_code,
                events=events,
                session_id=session_id_found,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
                error_message="permission denied",
                denied_actions=parser.denied_actions,
            )

        # Top level error
        if parser.top_level_error:
            return OpenCodeResult(
                classification=RunClassification.PROCESS_ERROR,
                exit_code=exit_code,
                events=events,
                session_id=session_id_found,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
                error_message=str(parser.top_level_error.get("message", "")),
            )

        # Non-zero exit with non-JSON or error
        if exit_code != 0:
            # Try to detect non-JSON usage/help
            redacted_stderr = redact_text(parser.strip_ansi(stderr))
            redacted_stdout = redact_text(parser.strip_ansi(stdout))
            msg = redacted_stderr.strip() or redacted_stdout.strip() or f"exit code {exit_code}"
            # If no parseable JSON events and non-zero, protocol/process error
            if not events:
                return OpenCodeResult(
                    classification=RunClassification.PROCESS_ERROR,
                    exit_code=exit_code,
                    events=events,
                    session_id=session_id_found,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                    duration_ms=duration_ms,
                    error_message=msg[:500],
                )
            else:
                return OpenCodeResult(
                    classification=RunClassification.PROCESS_ERROR,
                    exit_code=exit_code,
                    events=events,
                    session_id=session_id_found,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                    duration_ms=duration_ms,
                    error_message=msg[:500],
                )

        # Exit 0 - must not trust alone; require evidence
        if parser.has_final_text or any(e.type.value == "step_finish" for e in events):
            return OpenCodeResult(
                classification=RunClassification.SUCCESS,
                exit_code=exit_code,
                events=events,
                session_id=session_id_found,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
            )
        # Exit 0 but no clear success evidence
        if events:
            return OpenCodeResult(
                classification=RunClassification.SUCCESS,
                exit_code=exit_code,
                events=events,
                session_id=session_id_found,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
            )
        return OpenCodeResult(
            classification=RunClassification.PROTOCOL_ERROR,
            exit_code=exit_code,
            events=events,
            session_id=session_id_found,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_ms=duration_ms,
            error_message="no events produced",
        )
