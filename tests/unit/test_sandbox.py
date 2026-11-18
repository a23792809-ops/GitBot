"""Tests for the strong read-only repository sandbox foundation."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest

from gitbot.security.sandbox import (
    SandboxUnavailable,
    SnapshotError,
    build_sandbox_command,
    create_repository_snapshot,
    detect_sandbox_capabilities,
    extract_archive,
    isolated_repository,
    require_sandbox,
    run_sandbox_command,
    validate_source_repository,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "GitBot Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "app.py").write_text("print('one')\n", encoding="utf-8")
    (path / ".gitignore").write_text(".env\n", encoding="utf-8")
    os.symlink("app.py", path / "app-link")
    _git(path, "add", "app.py", ".gitignore", "app-link")
    _git(path, "commit", "-q", "-m", "initial")
    # Ignored and untracked files must never reach the snapshot.
    (path / ".env").write_text("SECRET=not-for-the-model\n", encoding="utf-8")
    return path


def test_detects_this_machines_capabilities() -> None:
    caps = detect_sandbox_capabilities()
    if not caps.can_run:
        pytest.skip(f"bubblewrap sandbox unavailable: {caps.detail}")
    assert caps.bwrap_path
    assert caps.version and "bubblewrap" in caps.version.lower()
    assert caps.user_namespaces is True
    assert caps.filesystem_namespaces is True
    assert caps.network_namespaces is True


def test_missing_bwrap_fails_closed() -> None:
    caps = detect_sandbox_capabilities(bwrap="/definitely/not/bwrap")
    assert caps.can_run is False
    with pytest.raises(SandboxUnavailable):
        require_sandbox(bwrap="/definitely/not/bwrap")


def test_source_validation_rejects_outside_allowlist(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(Exception, match="allowed roots"):
        validate_source_repository(repo, allowed_roots=[tmp_path / "elsewhere"])


def test_snapshot_contains_tracked_files_without_git_or_ignored_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    with create_repository_snapshot(repo, temporary_root=tmp_path) as snapshot:
        assert (snapshot.root / "app.py").read_text(encoding="utf-8") == "print('one')\n"
        assert (snapshot.root / ".gitignore").is_file()
        assert not (snapshot.root / ".git").exists()
        assert not (snapshot.root / ".env").exists()
        assert not (snapshot.root / "app-link").exists()
        assert snapshot.stats.skipped_links == 1
        assert snapshot.source == repo.resolve()
        assert snapshot.head


def test_snapshot_requires_clean_working_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="must be clean"):
        create_repository_snapshot(repo, temporary_root=tmp_path)


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, mode="w") as handle:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"escape"
        info.size = len(payload)
        import io

        handle.addfile(info, io.BytesIO(payload))

    destination = tmp_path / "out"
    with pytest.raises(SnapshotError, match="traversal"):
        extract_archive(archive, destination)
    assert not (tmp_path / "escape.txt").exists()


def test_build_command_mounts_only_snapshot_and_workspace(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()

    command = build_sandbox_command(
        snapshot,
        workspace,
        ["/usr/bin/python3", "-c", "print('ok')"],
    )
    argv = list(command.argv)
    assert "--unshare-all" in argv
    assert "--clearenv" in argv
    assert "--ro-bind" in argv
    assert str(snapshot.resolve()) in argv
    assert "/repo" in argv
    assert str(workspace.resolve()) in argv
    assert "/work" in argv
    assert not any(path == "/home" or path.startswith("/home/") for path in argv)
    assert not any(path == "/etc" or path.startswith("/etc/") for path in argv)
    assert "--auto" not in argv
    assert "GH_TOKEN" not in command.env
    assert command.env["HOME"] == "/work/home"
    assert command.env["TMPDIR"] == "/work/tmp"
    assert command.cwd == "/repo"


def test_build_command_rejects_shells_and_credentials(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()

    with pytest.raises(SandboxUnavailable, match="shell"):
        build_sandbox_command(snapshot, workspace, ["/bin/sh", "-c", "id"])

    with pytest.raises(SandboxUnavailable, match="credential"):
        build_sandbox_command(
            snapshot,
            workspace,
            ["/usr/bin/python3", "-V"],
            extra_env={"GH_TOKEN": "not-real"},
        )

    with pytest.raises(SandboxUnavailable, match="reserved"):
        build_sandbox_command(
            snapshot,
            workspace,
            ["/usr/bin/python3", "-V"],
            extra_env={"home": "/home/om"},
        )


def test_build_command_binds_an_executable_outside_runtime_roots(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()
    executable = tmp_path / "fake-opencode"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    command = build_sandbox_command(snapshot, workspace, [str(executable)])
    argv = list(command.argv)
    assert str(executable) in argv
    assert "/run/gitbot/bin/fake-opencode" in argv
    assert command.argv[command.argv.index("--") + 1] == "/run/gitbot/bin/fake-opencode"


def test_extra_mount_sources_must_be_relay_tls_or_executable(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()
    home = tmp_path / "fake-home"
    home.mkdir()
    (home / "id_rsa").write_text("not a real key\n", encoding="utf-8")
    repo = _init_repo(tmp_path / "repo")

    for source, target in (
        (home, "/etc/hosts"),
        (home / "id_rsa", "/run/gitbot/bin/id_rsa"),
        (repo / "app.py", "/run/gitbot/bin/app.py"),
    ):
        with pytest.raises(SandboxUnavailable, match="source is not allowed"):
            build_sandbox_command(
                snapshot,
                workspace,
                ["/usr/bin/python3", "-V"],
                extra_ro_binds=[(source, target)],
            )

    with pytest.raises(SandboxUnavailable, match="writable"):
        build_sandbox_command(
            snapshot,
            workspace,
            ["/usr/bin/python3", "-V"],
            extra_rw_binds=[(home, "/etc/hosts")],
        )


def test_extra_mounts_allow_the_relay_script_and_tls_files(tmp_path: Path) -> None:
    from gitbot.security.sandbox import _RELAY_SCRIPT_SOURCE

    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()
    assert _RELAY_SCRIPT_SOURCE.is_file()

    binds: list[tuple[Path, str]] = [(_RELAY_SCRIPT_SOURCE, "/run/gitbot/bin/relay.py")]
    hosts = Path("/etc/hosts")
    if hosts.is_file():
        binds.append((hosts, "/etc/hosts"))

    command = build_sandbox_command(
        snapshot,
        workspace,
        ["/usr/bin/python3", "-V"],
        extra_ro_binds=binds,
    )
    assert str(_RELAY_SCRIPT_SOURCE.resolve()) in command.argv


def test_extra_sockets_require_a_private_gitbot_directory(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()

    def make_socket(directory: Path, name: str = "gateway.sock") -> Path:
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / name
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        try:
            return path
        finally:
            sock.close()

    good = make_socket(tmp_path / "gitbot-gateway")
    command = build_sandbox_command(
        snapshot,
        workspace,
        ["/usr/bin/python3", "-V"],
        extra_rw_binds=[(good, "/run/gitbot/gateway.sock")],
    )
    assert str(good.resolve()) in command.argv

    plain = make_socket(tmp_path / "plain-directory")
    with pytest.raises(SandboxUnavailable, match="GitBot-created private directory"):
        build_sandbox_command(
            snapshot,
            workspace,
            ["/usr/bin/python3", "-V"],
            extra_rw_binds=[(plain, "/run/gitbot/gateway.sock")],
        )

    open_dir = tmp_path / "gitbot-open"
    open_dir.mkdir(mode=0o700)
    open_dir.chmod(0o755)
    exposed = make_socket(open_dir)
    with pytest.raises(SandboxUnavailable, match="group/world"):
        build_sandbox_command(
            snapshot,
            workspace,
            ["/usr/bin/python3", "-V"],
            extra_rw_binds=[(exposed, "/run/gitbot/gateway.sock")],
        )

    with pytest.raises(SandboxUnavailable, match="writable"):
        build_sandbox_command(
            snapshot,
            workspace,
            ["/usr/bin/python3", "-V"],
            extra_ro_binds=[(good, "/run/gitbot/gateway.sock")],
        )


def test_snapshot_ignores_repository_fsmonitor_commands(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    marker = tmp_path / "fsmonitor-ran"
    _git(repo, "config", "core.fsmonitor", f"touch {marker}")

    with create_repository_snapshot(repo, temporary_root=tmp_path) as snapshot:
        assert (snapshot.root / "app.py").is_file()

    assert not marker.exists()


def test_extra_non_secret_env_is_passed_inside_sandbox(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "work"
    snapshot.mkdir()
    workspace.mkdir()

    command = build_sandbox_command(
        snapshot,
        workspace,
        ["/usr/bin/python3", "-V"],
        extra_env={"OPENCODE_CONFIG_CONTENT": '{"permissions":[]}'},
    )
    assert command.env["OPENCODE_CONFIG_CONTENT"] == '{"permissions":[]}'


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_sandbox_blocks_writes_host_reads_and_network(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    script = r"""
import json, os, socket
result = {
    "cwd": os.getcwd(),
    "source": open("/repo/app.py", encoding="utf-8").read().strip(),
    "gh_token": os.environ.get("GH_TOKEN"),
}
try:
    open("/repo/blocked.txt", "w", encoding="utf-8").write("nope")
    result["repo_write"] = "allowed"
except OSError:
    result["repo_write"] = "denied"
try:
    open("/work/allowed.txt", "w", encoding="utf-8").write("ok")
    result["work_write"] = "allowed"
except OSError:
    result["work_write"] = "denied"
for path in ("/home", "/root", "/etc", "/etc/shadow"):
    try:
        os.listdir(path)
        result["host_" + path.replace("/", "_")] = "allowed"
    except OSError:
        result["host_" + path.replace("/", "_")] = "denied"
try:
    socket.create_connection(("1.1.1.1", 80), timeout=1).close()
    result["network"] = "allowed"
except OSError:
    result["network"] = "denied"
print(json.dumps(result))
"""
    snapshot_path: Path | None = None
    workspace_path: Path | None = None

    with isolated_repository(
        repo,
        allowed_roots=[tmp_path],
        command=["/usr/bin/python3", "-c", script],
        temporary_root=tmp_path,
    ) as sandbox:
        snapshot_path = sandbox.snapshot.root
        workspace_path = sandbox.workspace.root
        completed = subprocess.run(
            sandbox.command.argv,
            env=dict(sandbox.command.host_env),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["cwd"] == "/repo"
        assert result["source"] == "print('one')"
        assert result["gh_token"] is None
        assert result["repo_write"] == "denied"
        assert result["work_write"] == "allowed"
        assert result["host__home"] == "denied"
        assert result["host__root"] == "denied"
        assert result["host__etc"] == "denied"
        assert result["host__etc_shadow"] == "denied"
        assert result["network"] == "denied"

    assert snapshot_path is not None and not snapshot_path.exists()
    assert workspace_path is not None and not workspace_path.exists()
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('one')\n"
    assert not (repo / "blocked.txt").exists()


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_cleanup_happens_after_an_exception(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    snapshot_path: Path | None = None
    workspace_path: Path | None = None
    with (
        pytest.raises(RuntimeError),
        isolated_repository(
            repo,
            allowed_roots=[tmp_path],
            command=["/usr/bin/python3", "-V"],
            temporary_root=tmp_path,
        ) as sandbox,
    ):
        snapshot_path = sandbox.snapshot.root
        workspace_path = sandbox.workspace.root
        raise RuntimeError("boom")

    assert snapshot_path is not None and not snapshot_path.exists()
    assert workspace_path is not None and not workspace_path.exists()


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_run_sandbox_command_bounds_output(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    with isolated_repository(
        repo,
        allowed_roots=[tmp_path],
        command=["/usr/bin/python3", "-c", "print('x' * 5000)"],
        temporary_root=tmp_path,
    ) as sandbox:
        result = run_sandbox_command(sandbox.command, timeout_s=30, max_output_bytes=64)
    assert result.ok
    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert len(result.stdout) == 64
    assert result.stderr_truncated is False


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_run_sandbox_command_enforces_timeout(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    started = time.monotonic()
    with isolated_repository(
        repo,
        allowed_roots=[tmp_path],
        command=["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
        temporary_root=tmp_path,
    ) as sandbox:
        result = run_sandbox_command(
            sandbox.command,
            timeout_s=0.5,
            kill_grace_s=1.0,
        )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.exit_code not in (0, None)
    assert elapsed < 15


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_run_sandbox_command_honours_cancellation(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        with isolated_repository(
            repo,
            allowed_roots=[tmp_path],
            command=["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
            temporary_root=tmp_path,
        ) as sandbox:
            result = run_sandbox_command(
                sandbox.command,
                timeout_s=30,
                cancel_event=cancel,
                kill_grace_s=1.0,
            )
    finally:
        timer.cancel()
    assert result.cancelled is True
    assert result.timed_out is False
    assert result.duration_ms < 10_000


@pytest.mark.skipif(
    not detect_sandbox_capabilities().can_run,
    reason="strong sandbox unavailable",
)
def test_run_sandbox_command_escalates_after_ignored_sigterm(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    try:
        with isolated_repository(
            repo,
            allowed_roots=[tmp_path],
            command=["/usr/bin/python3", "-c", script],
            temporary_root=tmp_path,
        ) as sandbox:
            started = time.monotonic()
            result = run_sandbox_command(
                sandbox.command,
                timeout_s=30,
                cancel_event=cancel,
                kill_grace_s=0.5,
            )
            elapsed = time.monotonic() - started
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.exit_code not in (0, None)
    assert elapsed < 10


def test_real_opencode_debug_paths_stay_inside_work(tmp_path: Path) -> None:
    opencode = shutil.which("opencode")
    if opencode is None:
        pytest.skip("opencode executable not available")
    if not detect_sandbox_capabilities().can_run:
        pytest.skip("strong sandbox unavailable")

    repo = _init_repo(tmp_path / "repo")
    policy = {
        "$schema": "https://opencode.ai/config.json",
        "permissions": [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "read", "resource": "*", "effect": "allow"},
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "shell", "resource": "*", "effect": "deny"},
            {"action": "external_directory", "resource": "*", "effect": "deny"},
        ],
    }
    with isolated_repository(
        repo,
        allowed_roots=[tmp_path],
        command=[opencode, "debug", "paths"],
        extra_env={
            "OPENCODE_CONFIG_CONTENT": json.dumps(policy),
            "OPENCODE_CONFIG_DIR": "/work/config",
            "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        },
        temporary_root=tmp_path,
    ) as sandbox:
        completed = subprocess.run(
            sandbox.command.argv,
            env=dict(sandbox.command.host_env),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            _name, value = line.split(None, 1)
            assert value == "/work/home" or value.startswith("/work/")
        assert "/home/om" not in completed.stdout

    assert (repo / "app.py").read_text(encoding="utf-8") == "print('one')\n"
    assert not (repo / "opencode.json").exists()
