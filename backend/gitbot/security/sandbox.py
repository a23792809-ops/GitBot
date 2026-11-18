"""Strong read-only filesystem/network isolation for repository analysis.

This module is deliberately independent of the OpenCode adapter.  It builds a
bubblewrap sandbox around an arbitrary *fixed* command (never a shell supplied
by a model) and exposes the resulting argv to a caller.  The security boundary
is the mount namespace:

* the source repository is copied into a disposable snapshot;
* only the snapshot is mounted, read-only, at ``/repo``;
* the original repository, the user's home, and its ``.git`` directory are
  never mounted;
* a separate private directory is mounted read-write at ``/work``;
* user, PID, network, IPC, UTS, and cgroup namespaces are unshared;
* the environment is cleared and replaced with a small allowlist.

If bubblewrap, user namespaces, or the probe command are unavailable the
public entry point raises :class:`SandboxUnavailable`; there is intentionally
no weaker fallback.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gitbot.errors import PolicyViolation
from gitbot.security.allowlist import PROJECT_ROOT, resolve_repo_path
from gitbot.security.redact import contains_secret, redact_text

logger = logging.getLogger("gitbot.security.sandbox")

DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 200_000
DEFAULT_GIT_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 10.0
DEFAULT_RUN_TIMEOUT_S = 300.0
DEFAULT_KILL_GRACE_S = 5.0
DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SANDBOX_REPO = "/repo"
SANDBOX_WORK = "/work"
SANDBOX_EXEC_DIR = "/run/gitbot/bin"
SANDBOX_RUN_DIR = "/run/gitbot"

# Only these host paths are made visible.  Directories such as /home, /root,
# /etc (as a whole), /var, /mnt, /media, and /opt are never mounted.
_DEFAULT_RUNTIME_ROOTS = ("/usr", "/bin", "/lib", "/lib64", "/lib32", "/sbin")
_ALLOWED_EXTRA_ROOTS = (
    PurePosixPath("/run/gitbot"),
    PurePosixPath("/etc/ssl"),
    PurePosixPath("/etc/ca-certificates"),
    PurePosixPath("/etc/hosts"),
    PurePosixPath("/etc/resolv.conf"),
    PurePosixPath("/etc/nsswitch.conf"),
    PurePosixPath("/etc/passwd"),
    PurePosixPath("/etc/group"),
    PurePosixPath("/etc/localtime"),
)
# Read-only *sources* are even narrower than mount targets: only the relay
# script GitBot itself ships, the TLS trust files the host runner needs, and an
# executable mounted at SANDBOX_EXEC_DIR.  Home directories, repositories,
# credential stores, and arbitrary host paths are never valid sources.
_ALLOWED_EXTRA_RO_SOURCE_DIRS = (
    PurePosixPath("/etc/ssl"),
    PurePosixPath("/etc/ca-certificates"),
)
_ALLOWED_EXTRA_RO_SOURCE_FILES = (
    PurePosixPath("/etc/hosts"),
    PurePosixPath("/etc/resolv.conf"),
    PurePosixPath("/etc/nsswitch.conf"),
    PurePosixPath("/etc/passwd"),
    PurePosixPath("/etc/group"),
    PurePosixPath("/etc/localtime"),
)
_RELAY_SCRIPT_SOURCE = Path(__file__).resolve().parent / "sandbox_relay.py"
# Command-line configuration has higher precedence than a repository's
# ``.git/config``.  In particular, a malicious local ``core.fsmonitor`` value
# must never become a command GitBot executes while preparing a snapshot.
_SAFE_GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.pager=cat",
    "-c",
    "color.ui=false",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)

_DENIED_ENV_FRAGMENTS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PRIVATE",
    "API_KEY",
    "SSH",
    "AWS",
    "GITHUB",
    "GH_",
)
_SHELL_NAMES = frozenset({"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh"})
_RESERVED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TERM",
        "NO_COLOR",
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
    }
)


class SandboxUnavailable(PolicyViolation):
    """The required kernel/privilege facilities are not usable."""

    code = "sandbox_unavailable"
    http_status = 503


class SnapshotError(PolicyViolation):
    """The source repository could not be safely snapshotted."""

    code = "snapshot_error"
    http_status = 400


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    bwrap_path: str | None
    version: str | None
    user_namespaces: bool
    filesystem_namespaces: bool
    network_namespaces: bool
    can_run: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "bwrap_path": self.bwrap_path,
            "version": self.version,
            "user_namespaces": self.user_namespaces,
            "filesystem_namespaces": self.filesystem_namespaces,
            "network_namespaces": self.network_namespaces,
            "can_run": self.can_run,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExtractionStats:
    files: int
    directories: int
    bytes: int
    skipped_links: int


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """A fixed argv plus the two environments needed to execute it.

    ``env`` is the environment *inside* the sandbox (also rendered as
    ``--setenv`` arguments).  ``host_env`` is the minimal environment used for
    the bubblewrap process itself; it is never inherited by the child.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    host_env: Mapping[str, str]
    cwd: str = SANDBOX_REPO


@dataclass(frozen=True, slots=True)
class Sandbox:
    source: Path
    snapshot: RepositorySnapshot
    workspace: SandboxWorkspace
    command: SandboxCommand


@dataclass(frozen=True, slots=True)
class SandboxRunResult:
    """Bounded result of one sandboxed process execution."""

    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    cancelled: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


def _minimal_git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
    }


def _host_env() -> dict[str, str]:
    # bubblewrap itself needs a PATH; the child gets a separate clean env.
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LC_ALL": "C",
    }


def _bwrap_path(bwrap: str) -> str | None:
    if os.path.isabs(bwrap):
        return bwrap if Path(bwrap).is_file() else None
    return shutil.which(bwrap)


def _runtime_bind_args(runtime_roots: Sequence[Path]) -> list[str]:
    args: list[str] = []
    for root in runtime_roots:
        if root.is_dir():
            args.extend(["--ro-bind", str(root), str(root)])
    return args


def _probe_argv(bwrap_path: str) -> list[str]:
    runtime_roots = [Path(p) for p in _DEFAULT_RUNTIME_ROOTS]
    argv = [
        bwrap_path,
        "--unshare-all",
        "--unshare-user",
        "--die-with-parent",
        "--new-session",
        "--disable-userns",
        "--cap-drop",
        "ALL",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108 - private tmpfs, not the host /tmp
        *_runtime_bind_args(runtime_roots),
    ]
    true_path = Path("/usr/bin/true")
    if not true_path.is_file():
        true_path = Path("/bin/true")
    argv.extend(["--", str(true_path)])
    return argv


def detect_sandbox_capabilities(
    *, bwrap: str = "bwrap", timeout_s: float = PROBE_TIMEOUT_S
) -> SandboxCapabilities:
    """Probe bubblewrap and the namespaces this user can actually create."""
    path = _bwrap_path(bwrap)
    if path is None:
        return SandboxCapabilities(
            bwrap_path=None,
            version=None,
            user_namespaces=False,
            filesystem_namespaces=False,
            network_namespaces=False,
            can_run=False,
            detail="bubblewrap is not installed",
        )

    try:
        version_result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_host_env(),
        )
        version = (version_result.stdout or version_result.stderr or "").strip() or None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxCapabilities(
            bwrap_path=path,
            version=None,
            user_namespaces=False,
            filesystem_namespaces=False,
            network_namespaces=False,
            can_run=False,
            detail=f"bubblewrap version probe failed: {exc}",
        )

    try:
        probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
            _probe_argv(path),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_host_env(),
        )
        ok = probe.returncode == 0
        detail = "" if ok else redact_text((probe.stderr or "sandbox probe failed")[-800:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxCapabilities(
            bwrap_path=path,
            version=version,
            user_namespaces=False,
            filesystem_namespaces=False,
            network_namespaces=False,
            can_run=False,
            detail=f"sandbox probe failed: {exc}",
        )

    # --unshare-all includes user, PID, network, IPC, UTS, and cgroup
    # namespaces.  A successful probe is therefore evidence for all three
    # capability flags rather than a weaker partial fallback.
    return SandboxCapabilities(
        bwrap_path=path,
        version=version,
        user_namespaces=ok,
        filesystem_namespaces=ok,
        network_namespaces=ok,
        can_run=ok,
        detail=detail,
    )


def require_sandbox(*, bwrap: str = "bwrap") -> SandboxCapabilities:
    """Return capabilities or raise a fail-closed error."""
    capabilities = detect_sandbox_capabilities(bwrap=bwrap)
    if not capabilities.can_run:
        raise SandboxUnavailable(
            "strong read-only sandbox is unavailable on this machine",
            details=capabilities.to_dict(),
        )
    return capabilities


def validate_source_repository(
    raw: str | os.PathLike[str],
    *,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (PROJECT_ROOT,),
) -> Path:
    """Validate a user-selected repository before it is snapshotted."""
    resolved = resolve_repo_path(
        raw,
        allowed_roots=allowed_roots,
        forbidden_roots=forbidden_roots,
        require_git=True,
    )
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise SnapshotError(
            "source repository is not readable",
            details={"path": str(resolved)},
        )
    return resolved


def _run_git(source: Path, args: Sequence[str], *, timeout_s: float = DEFAULT_GIT_TIMEOUT_S) -> str:
    argv = ["git", *_SAFE_GIT_CONFIG, "-C", str(source), *args]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_minimal_git_env(),
        )
    except FileNotFoundError as exc:
        raise SnapshotError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError("git command timed out while creating the snapshot") from exc
    if completed.returncode != 0:
        raise SnapshotError(
            f"git {args[0]} failed while creating the snapshot",
            details={
                "exit_code": completed.returncode,
                "stderr": redact_text(completed.stderr or "")[-800:],
            },
        )
    return completed.stdout or ""


def _safe_member_destination(root: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if not pure.parts:
        raise SnapshotError("archive contains an empty path")
    if pure.is_absolute() or any(part in {"", ".."} for part in pure.parts):
        raise SnapshotError(
            "archive contains a path traversal attempt",
            details={"member": redact_text(name)},
        )
    destination = root.joinpath(*pure.parts)
    try:
        destination.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise SnapshotError(
            "archive member escapes the snapshot root",
            details={"member": redact_text(name)},
        ) from exc
    return destination


def extract_archive(
    archive_path: Path | str,
    destination: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> ExtractionStats:
    """Extract a tar archive without links, devices, or traversal.

    Symlinks and hardlinks are skipped rather than materialized: following a
    link inside the sandbox could expose a host path or another snapshot path.
    """
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    entries = 0
    files = 0
    directories = 0
    total = 0
    skipped_links = 0

    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            entries += 1
            if entries > max_entries:
                raise SnapshotError("snapshot exceeds the maximum entry count")
            target = _safe_member_destination(root, member.name)

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                directories += 1
                continue
            if member.issym() or member.islnk():
                skipped_links += 1
                continue
            if not member.isfile():
                skipped_links += 1
                continue

            total += member.size
            if total > max_bytes:
                raise SnapshotError("snapshot exceeds the maximum extracted size")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as handle:
                shutil.copyfileobj(source, handle, length=64 * 1024)
            os.chmod(target, 0o444 | (member.mode & 0o111))
            files += 1

    return ExtractionStats(
        files=files,
        directories=directories,
        bytes=total,
        skipped_links=skipped_links,
    )


class RepositorySnapshot:
    """A disposable, tracked-files-only copy of a clean repository HEAD."""

    def __init__(
        self,
        *,
        root: Path,
        source: Path,
        head: str,
        stats: ExtractionStats,
        temporary: tempfile.TemporaryDirectory[str],
    ) -> None:
        self.root = root
        self.source = source
        self.head = head
        self.stats = stats
        self._temporary = temporary
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._temporary.cleanup()

    def __enter__(self) -> RepositorySnapshot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def create_repository_snapshot(
    source: Path | str,
    *,
    temporary_root: Path | str | None = None,
    require_clean: bool = True,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> RepositorySnapshot:
    """Create a snapshot from ``git archive HEAD`` without touching the source."""
    resolved = Path(source).resolve(strict=True)
    if require_clean:
        status = _run_git(resolved, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if status.replace("\x00", "").strip():
            raise SnapshotError(
                "source repository must be clean before a read-only snapshot",
                details={"path": str(resolved)},
            )

    head = _run_git(resolved, ["rev-parse", "HEAD"]).strip()
    if not head:
        raise SnapshotError("source repository has no HEAD commit")

    temp = tempfile.TemporaryDirectory(prefix="gitbot-snapshot-", dir=temporary_root)
    try:
        temporary = Path(temp.name)
        os.chmod(temporary, 0o700)
        archive_path = temporary / "snapshot.tar"
        snapshot_root = temporary / "repo"
        snapshot_root.mkdir(mode=0o700)

        with archive_path.open("wb") as archive_handle:
            argv = [
                "git",
                *_SAFE_GIT_CONFIG,
                "-C",
                str(resolved),
                "archive",
                "--format=tar",
                "HEAD",
            ]
            try:
                completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    argv,
                    stdout=archive_handle,
                    stderr=subprocess.PIPE,
                    timeout=DEFAULT_GIT_TIMEOUT_S,
                    check=False,
                    env=_minimal_git_env(),
                )
            except FileNotFoundError as exc:
                raise SnapshotError("git executable not found") from exc
            except subprocess.TimeoutExpired as exc:
                raise SnapshotError("git archive timed out while creating the snapshot") from exc
        if completed.returncode != 0:
            raise SnapshotError(
                "git archive failed while creating the snapshot",
                details={
                    "exit_code": completed.returncode,
                    "stderr": redact_text((completed.stderr or b"").decode("utf-8", "replace"))[
                        -800:
                    ],
                },
            )
        if archive_path.stat().st_size > max_archive_bytes:
            raise SnapshotError("snapshot archive exceeds the maximum size")

        stats = extract_archive(
            archive_path,
            snapshot_root,
            max_bytes=max_extracted_bytes,
            max_entries=max_entries,
        )
        archive_path.unlink(missing_ok=True)
        return RepositorySnapshot(
            root=snapshot_root,
            source=resolved,
            head=head,
            stats=stats,
            temporary=temp,
        )
    except BaseException:
        temp.cleanup()
        raise


class SandboxWorkspace:
    """Private writable directory mounted at ``/work`` inside the sandbox."""

    def __init__(self, root: Path, temporary: tempfile.TemporaryDirectory[str]) -> None:
        self.root = root
        self._temporary = temporary
        self._closed = False

    @classmethod
    def create(cls, *, temporary_root: Path | str | None = None) -> SandboxWorkspace:
        temp = tempfile.TemporaryDirectory(prefix="gitbot-sandbox-work-", dir=temporary_root)
        try:
            root = Path(temp.name)
            os.chmod(root, 0o700)
            for name in ("home", "tmp", "cache", "config", "data", "state"):
                child = root / name
                child.mkdir(mode=0o700)
                os.chmod(child, 0o700)
        except BaseException:
            temp.cleanup()
            raise
        return cls(root, temp)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._temporary.cleanup()

    def __enter__(self) -> SandboxWorkspace:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _validate_extra_env(extra_env: Mapping[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in extra_env.items():
        upper = key.upper()
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise SandboxUnavailable(f"invalid sandbox environment variable: {key!r}")
        if upper in _RESERVED_ENV:
            raise SandboxUnavailable(f"refusing to override reserved sandbox variable: {key}")
        if any(fragment in upper for fragment in _DENIED_ENV_FRAGMENTS):
            raise SandboxUnavailable(
                f"refusing to pass a credential-like environment variable: {key}"
            )
        if contains_secret(value):
            raise SandboxUnavailable(f"refusing to pass a secret-shaped value for {key}")
        clean[key] = value
    return clean


def _child_env(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": f"{SANDBOX_EXEC_DIR}:/usr/local/bin:/usr/bin:/bin",
        "HOME": f"{SANDBOX_WORK}/home",
        "TMPDIR": f"{SANDBOX_WORK}/tmp",  # noqa: S108 - path inside the sandbox
        "XDG_CACHE_HOME": f"{SANDBOX_WORK}/cache",
        "XDG_CONFIG_HOME": f"{SANDBOX_WORK}/config",
        "XDG_DATA_HOME": f"{SANDBOX_WORK}/data",
        "XDG_STATE_HOME": f"{SANDBOX_WORK}/state",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    if extra_env:
        env.update(_validate_extra_env(extra_env))
    return env


def _resolve_executable(command: Sequence[str]) -> Path:
    if not command:
        raise SandboxUnavailable("sandbox command cannot be empty")
    raw = command[0]
    candidate = Path(raw)
    found = str(candidate.resolve(strict=True)) if candidate.is_absolute() else shutil.which(raw)
    if not found:
        raise SandboxUnavailable(f"sandbox command executable not found: {raw!r}")
    resolved = Path(found).resolve(strict=True)
    if not resolved.is_file():
        raise SandboxUnavailable(f"sandbox command executable is not a file: {resolved}")
    return resolved


def _is_under(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _validate_sandbox_cwd(cwd: str) -> str:
    """Only the two intended sandbox roots may be used as the working directory."""
    path = PurePosixPath(cwd)
    if not path.is_absolute():
        raise SandboxUnavailable("sandbox working directory must be absolute")
    allowed_roots = {PurePosixPath(SANDBOX_REPO), PurePosixPath(SANDBOX_WORK)}
    if not any(path == root or path.is_relative_to(root) for root in allowed_roots):
        raise SandboxUnavailable("sandbox working directory is outside /repo and /work")
    return str(path)


def _is_allowed_read_only_source(path: Path) -> bool:
    """True only for the relay script and the fixed TLS trust files."""
    if path == _RELAY_SCRIPT_SOURCE:
        return True
    for allowed in _ALLOWED_EXTRA_RO_SOURCE_DIRS:
        try:
            path.relative_to(Path(allowed).resolve(strict=False))
            return True
        except ValueError:
            continue
    return any(
        path == Path(allowed).resolve(strict=False) for allowed in _ALLOWED_EXTRA_RO_SOURCE_FILES
    )


def _validate_private_socket(path: Path) -> None:
    """Accept only a GitBot-created socket inside a private temporary directory.

    This is what lets the sandbox reach the gateway/tunnel without exposing a
    host socket such as ssh-agent, gpg-agent, or the system D-Bus.  The socket
    name, its private parent directory (0700, current user), and the socket
    owner must all match the shape GitBot creates itself.
    """
    if not path.name.endswith(".sock"):
        raise SandboxUnavailable("extra sandbox socket must end with '.sock'")
    parent = path.parent
    try:
        parent_stat = parent.stat()
        socket_stat = path.stat()
    except OSError as exc:
        raise SandboxUnavailable("extra sandbox socket cannot be inspected") from exc
    if not parent.name.startswith("gitbot-"):
        raise SandboxUnavailable(
            "extra sandbox socket must live in a GitBot-created private directory"
        )
    if parent_stat.st_uid != os.geteuid() or socket_stat.st_uid != os.geteuid():
        raise SandboxUnavailable("extra sandbox socket is not owned by the current user")
    if parent_stat.st_mode & 0o077:
        raise SandboxUnavailable(
            "extra sandbox socket directory must not be group/world accessible"
        )


def _validate_extra_bind(source: Path | str, target: str, *, writable: bool) -> tuple[Path, str]:
    """Validate both ends of the narrow set of additional mounts GitBot may need.

    The normal analysis lane does not use this.  The AI gateway needs two
    things that are still inside the security boundary: a read-only relay
    script (or an executable) and a single private Unix socket.  TLS trust files
    are exposed read-only for the host-side model runner.  No caller may mount a
    home directory, a repository, a credential store, or an arbitrary host path,
    and no caller may bind a host directory writable.

    Destination paths must already be one of the allowed sandbox roots, so a
    valid source can never be redirected somewhere new.
    """
    path = Path(source).resolve(strict=True)
    destination = PurePosixPath(target)
    if not destination.is_absolute() or destination == PurePosixPath("/"):
        raise SandboxUnavailable("extra sandbox mount target must be an absolute file path")
    if ".." in destination.parts:
        raise SandboxUnavailable("extra sandbox mount target cannot contain '..'")
    if not any(
        destination == allowed or destination.is_relative_to(allowed)
        for allowed in _ALLOWED_EXTRA_ROOTS
    ):
        raise SandboxUnavailable(f"extra sandbox mount target is not allowed: {target}")

    if path.is_socket():
        if not writable:
            raise SandboxUnavailable("Unix sockets must use a writable sandbox mount")
        _validate_private_socket(path)
        return path, str(destination)

    if writable:
        # Only the gateway/tunnel sockets are writable extra mounts; a writable
        # directory bind would defeat the "no host path is writable" guarantee.
        raise SandboxUnavailable("only private gateway sockets may be mounted writable")
    if not path.is_file():
        if path.is_dir() and _is_allowed_read_only_source(path):
            return path, str(destination)
        raise SandboxUnavailable(f"extra sandbox mount source is not allowed: {path}")
    if os.access(path, os.X_OK) and destination.is_relative_to(PurePosixPath(SANDBOX_EXEC_DIR)):
        return path, str(destination)
    if not _is_allowed_read_only_source(path):
        raise SandboxUnavailable(f"extra sandbox mount source is not allowed: {path}")
    return path, str(destination)


def build_sandbox_command(
    snapshot_root: Path | str,
    writable_root: Path | str,
    command: Sequence[str],
    *,
    bwrap: str = "bwrap",
    extra_env: Mapping[str, str] | None = None,
    cwd: str = SANDBOX_REPO,
    extra_ro_binds: Sequence[tuple[Path | str, str]] = (),
    extra_rw_binds: Sequence[tuple[Path | str, str]] = (),
) -> SandboxCommand:
    """Build the fixed bwrap argv and restricted environments.

    The command must be an argv list chosen by GitBot, never a shell string.
    Shell interpreters are rejected unless the caller changes this module's
    policy explicitly; there is intentionally no parameter to do so.

    ``cwd`` may point at ``/repo`` (read-only) or a path under ``/work`` (the
    private writable workspace). It can never reference a host path.
    """
    snapshot = Path(snapshot_root)
    writable = Path(writable_root)
    if not snapshot.is_dir():
        raise SandboxUnavailable(f"snapshot directory does not exist: {snapshot}")
    if not writable.is_dir():
        raise SandboxUnavailable(f"workspace directory does not exist: {writable}")
    if writable.resolve().is_relative_to(snapshot.resolve()):
        raise SandboxUnavailable("writable workspace cannot be inside the read-only snapshot")
    sandbox_cwd = _validate_sandbox_cwd(cwd)

    bwrap_path = _bwrap_path(bwrap)
    if bwrap_path is None:
        raise SandboxUnavailable("bubblewrap is not installed")

    argv_command = list(command)
    executable = _resolve_executable(argv_command)
    if executable.name in _SHELL_NAMES:
        raise SandboxUnavailable("shell interpreters are not permitted inside the sandbox")
    if any(arg == "--auto" or arg.startswith("--auto=") for arg in argv_command):
        raise SandboxUnavailable("opencode --auto is not permitted")

    runtime_roots = [Path(path) for path in _DEFAULT_RUNTIME_ROOTS if Path(path).is_dir()]
    child_env = _child_env(extra_env)
    args = [
        bwrap_path,
        "--unshare-all",
        "--unshare-user",
        "--die-with-parent",
        "--new-session",
        "--disable-userns",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--hostname",
        "gitbot-sandbox",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108 - private tmpfs, not the host /tmp
        *_runtime_bind_args(runtime_roots),
        "--dir",
        "/run",
        "--dir",
        SANDBOX_RUN_DIR,
        "--dir",
        SANDBOX_EXEC_DIR,
    ]

    for source, target in extra_ro_binds:
        path, destination = _validate_extra_bind(source, target, writable=False)
        args.extend(["--ro-bind", str(path), destination])
    for source, target in extra_rw_binds:
        path, destination = _validate_extra_bind(source, target, writable=True)
        args.extend(["--bind", str(path), destination])

    if not _is_under(executable, runtime_roots):
        target = f"{SANDBOX_EXEC_DIR}/{executable.name}"
        args.extend(["--ro-bind", str(executable), target])
        argv_command[0] = target

    for name in ("home", "tmp", "cache", "config", "data", "state"):
        (writable / name).mkdir(mode=0o700, exist_ok=True)
        os.chmod(writable / name, 0o700)

    args.extend(
        [
            "--ro-bind",
            str(snapshot.resolve()),
            SANDBOX_REPO,
            "--bind",
            str(writable.resolve()),
            SANDBOX_WORK,
            "--chdir",
            sandbox_cwd,
        ]
    )
    for key, value in child_env.items():
        args.extend(["--setenv", key, value])
    args.extend(["--", *argv_command])
    return SandboxCommand(
        argv=tuple(args),
        env=child_env,
        host_env=_host_env(),
        cwd=sandbox_cwd,
    )


def _terminate_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the sandbox leader and every descendant in its process group."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:  # pragma: no cover - a new session should own the group
        process.send_signal(sig)


def _bounded_text(raw: bytes, max_bytes: int) -> tuple[str, bool]:
    if len(raw) <= max_bytes:
        return raw.decode("utf-8", "replace"), False
    return raw[:max_bytes].decode("utf-8", "replace"), True


def run_sandbox_command(
    spec: SandboxCommand,
    *,
    timeout_s: float | None = DEFAULT_RUN_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    kill_grace_s: float = DEFAULT_KILL_GRACE_S,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> SandboxRunResult:
    """Execute one fixed sandbox argv with a hard timeout and cancellation.

    The bwrap process starts a new session and is also ``--die-with-parent``;
    termination signals the whole process group so a model-started child cannot
    outlive the run.  Both pipes are drained continuously, but retained bytes
    are capped, so a noisy process cannot exhaust memory or force unbounded
    JSON/log storage.  Nothing from the result is logged here; callers must
    redact before persisting or displaying it.
    """
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive or None")
    if kill_grace_s < 0:
        raise ValueError("kill_grace_s must be non-negative")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")

    started = time.monotonic()
    if cancel_event is not None and cancel_event.is_set():
        return SandboxRunResult(
            exit_code=None,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            cancelled=True,
            duration_ms=0,
        )

    try:
        process = subprocess.Popen(  # noqa: S603 - argv comes from build_sandbox_command
            spec.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(spec.host_env),
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise SandboxUnavailable(f"failed to start the sandbox process: {exc}") from exc

    cancelled = False
    stop_watchdog = threading.Event()
    watchdog: threading.Thread | None = None

    def watch_for_cancel() -> None:
        nonlocal cancelled
        while not stop_watchdog.wait(0.1):
            if process.poll() is not None:
                return
            if cancel_event is None or not cancel_event.is_set():
                continue
            cancelled = True
            _terminate_process_group(process, signal.SIGTERM)
            deadline = time.monotonic() + kill_grace_s
            while time.monotonic() < deadline and process.poll() is None:
                stop_watchdog.wait(0.05)
            if process.poll() is None:
                _terminate_process_group(process, signal.SIGKILL)
            return

    if cancel_event is not None:
        watchdog = threading.Thread(
            target=watch_for_cancel,
            name="gitbot-sandbox-cancel",
            daemon=True,
        )
        watchdog.start()

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    truncated = [False, False]

    def drain(stream: object, buffer: bytearray, index: int) -> None:
        # Always drain the pipe so the child cannot block on a full pipe, but
        # stop retaining bytes once the cap is reached. This keeps memory
        # bounded even if a model-controlled process floods stdout.
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                room = max_output_bytes - len(buffer)
                if room > 0:
                    buffer.extend(chunk[:room])
                if len(chunk) > room:
                    truncated[index] = True
        finally:
            stream.close()  # type: ignore[attr-defined]

    readers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_buffer, 0),
            name="gitbot-sandbox-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_buffer, 1),
            name="gitbot-sandbox-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=kill_grace_s)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process, signal.SIGKILL)
                process.wait()
    finally:
        stop_watchdog.set()
        if watchdog is not None:
            watchdog.join(timeout=max(kill_grace_s + 1.0, 1.0))
        for reader in readers:
            reader.join(timeout=max(kill_grace_s + 1.0, 1.0))

    if cancel_event is not None and cancel_event.is_set():
        cancelled = True
    stdout, _ = _bounded_text(bytes(stdout_buffer), max_output_bytes)
    stderr, _ = _bounded_text(bytes(stderr_buffer), max_output_bytes)
    stdout_truncated = truncated[0]
    stderr_truncated = truncated[1]
    return SandboxRunResult(
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@contextmanager
def isolated_repository(
    source: Path | str,
    *,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (PROJECT_ROOT,),
    command: Sequence[str],
    extra_env: Mapping[str, str] | None = None,
    extra_ro_binds: Sequence[tuple[Path | str, str]] = (),
    extra_rw_binds: Sequence[tuple[Path | str, str]] = (),
    temporary_root: Path | str | None = None,
    bwrap: str = "bwrap",
    require_clean: bool = True,
    cwd: str = SANDBOX_REPO,
) -> Iterator[Sandbox]:
    """Create snapshot + workspace + command, and always clean both up."""
    require_sandbox(bwrap=bwrap)
    resolved = validate_source_repository(
        source,
        allowed_roots=allowed_roots,
        forbidden_roots=forbidden_roots,
    )
    snapshot = create_repository_snapshot(
        resolved,
        temporary_root=temporary_root,
        require_clean=require_clean,
    )
    workspace: SandboxWorkspace | None = None
    try:
        workspace = SandboxWorkspace.create(temporary_root=temporary_root)
        command_spec = build_sandbox_command(
            snapshot.root,
            workspace.root,
            command,
            bwrap=bwrap,
            extra_env=extra_env,
            extra_ro_binds=extra_ro_binds,
            extra_rw_binds=extra_rw_binds,
            cwd=cwd,
        )
        yield Sandbox(
            source=resolved,
            snapshot=snapshot,
            workspace=workspace,
            command=command_spec,
        )
    finally:
        if workspace is not None:
            workspace.close()
        snapshot.close()
