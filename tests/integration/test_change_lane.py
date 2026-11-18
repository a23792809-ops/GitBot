"""Integration tests for the sandboxed code-change lane.

The tests use the real bubblewrap sandbox and a hermetic fake ``opencode``.
They never touch a pre-existing repository, database, or credential.
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from gitbot.errors import PolicyViolation
from gitbot.opencode.adapter import OpenCodeConfig, RunClassification
from gitbot.opencode.changes import run_change
from gitbot.security.sandbox import SandboxUnavailable


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "GitBot Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    (path / "old.txt").write_text("remove me\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=path, check=True)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _prompt(**directives: object) -> str:
    import json

    return "change the repository GITBOT_FAKE:" + json.dumps(directives) + "]]"


def test_change_diff_is_accurate_and_source_repository_is_untouched(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    before = _git(source, "rev-parse", "HEAD").strip()
    sandboxes = tmp_path / "sandboxes"
    sandboxes.mkdir()

    fake = write_fake_opencode()
    prompt = _prompt(
        writes={"new.txt": "new file\n", "nested/deep.txt": "deep\n"},
        binary={"blob.bin": base64.b64encode(b"\x00\x01\x02binary").decode("ascii")},
        deletes=["old.txt"],
    )
    result = run_change(
        source,
        prompt,
        allowed_roots=(tmp_path,),
        config=OpenCodeConfig(executable=str(fake), timeout_s=20),
        temporary_root=sandboxes,
    )

    assert result.classification is RunClassification.SUCCESS
    assert result.diff_truncated is False
    assert set(result.files) == {"new.txt", "nested/deep.txt", "blob.bin", "old.txt"}
    assert "new.txt" in result.patch
    assert "blob.bin" in result.patch
    assert "old.txt" in result.patch

    # The source repository is unchanged: same HEAD, clean worktree, no files.
    assert _git(source, "rev-parse", "HEAD").strip() == before
    assert _git(source, "status", "--porcelain").strip() == ""
    assert not (source / "new.txt").exists()
    assert not (source / "blob.bin").exists()
    assert (source / "old.txt").read_text(encoding="utf-8") == "remove me\n"

    # The generated patch is a real, applicable diff, not just selected text.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(source), str(clone)], check=True)
    patch_file = tmp_path / "review.patch"
    patch_file.write_text(result.patch, encoding="utf-8")
    subprocess.run(
        ["git", "apply", "--binary", str(patch_file)],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    assert (clone / "new.txt").read_text(encoding="utf-8") == "new file\n"
    assert (clone / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep\n"
    assert (clone / "blob.bin").read_bytes() == b"\x00\x01\x02binary"
    assert not (clone / "old.txt").exists()

    # Snapshot and workspace are removed by the context manager.
    assert list(sandboxes.iterdir()) == []


def test_change_lane_does_not_expose_original_git_directory(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    fake = write_fake_opencode()
    prompt = _prompt(
        probes=["/repo/.git", "/work/repo/.git", "/repo/README.md", "/work/repo/README.md"]
    )
    result = run_change(
        source,
        prompt,
        allowed_roots=(tmp_path,),
        config=OpenCodeConfig(executable=str(fake), timeout_s=20),
    )

    visibility = json.loads(result.output or "{}")
    assert visibility == {
        "/repo/.git": False,
        "/work/repo/.git": False,
        "/repo/README.md": True,
        "/work/repo/README.md": True,
    }
    assert result.patch == ""
    assert _git(source, "status", "--porcelain").strip() == ""


def test_change_lane_rejects_symlink_changes(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    fake = write_fake_opencode()
    prompt = _prompt(symlinks={"escape": "/etc/passwd"})

    with pytest.raises(PolicyViolation, match="symlink or submodule"):
        run_change(
            source,
            prompt,
            allowed_roots=(tmp_path,),
            config=OpenCodeConfig(executable=str(fake), timeout_s=20),
        )

    assert not (source / "escape").exists()
    assert _git(source, "status", "--porcelain").strip() == ""


def test_change_lane_runs_configured_tests_in_the_sandbox(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    fake = write_fake_opencode()
    command = (
        "python3 -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('new.txt').exists() else 1)\""
    )

    result = run_change(
        source,
        _prompt(writes={"new.txt": "new file\n"}),
        allowed_roots=(tmp_path,),
        config=OpenCodeConfig(executable=str(fake), timeout_s=20),
        test_command=command,
    )

    assert result.classification is RunClassification.SUCCESS
    assert result.tests_status == "passed"
    assert result.tests_command == command
    assert result.test_exit_code == 0
    assert result.tests_output is None
    assert "new.txt" in result.patch
    # Test execution and artifact capture never write to the source repository.
    assert _git(source, "status", "--porcelain").strip() == ""


def test_change_lane_records_failed_tests(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    fake = write_fake_opencode()

    result = run_change(
        source,
        _prompt(writes={"new.txt": "new file\n"}),
        allowed_roots=(tmp_path,),
        config=OpenCodeConfig(executable=str(fake), timeout_s=20),
        test_command='python3 -c "import sys; sys.exit(7)"',
    )

    assert result.classification is RunClassification.SUCCESS
    assert result.tests_status == "failed"
    assert result.test_exit_code == 7
    assert result.tests_output is None


def test_change_lane_rejects_shell_syntax_in_test_command(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    fake = write_fake_opencode()

    result = run_change(
        source,
        _prompt(writes={"new.txt": "new file\n"}),
        allowed_roots=(tmp_path,),
        config=OpenCodeConfig(executable=str(fake), timeout_s=20),
        test_command="python3 -c \"print('a')\" && true",
    )

    assert result.tests_status == "error"
    assert result.test_exit_code is None
    assert result.tests_output is not None
    assert "shell operators" in result.tests_output


def test_change_lane_fails_closed_without_bwrap(
    tmp_path: Path, write_fake_opencode: Callable[..., Path]
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    _init_git_repo(source)
    fake = write_fake_opencode()

    with pytest.raises(SandboxUnavailable):
        run_change(
            source,
            _prompt(writes={"new.txt": "no\n"}),
            allowed_roots=(tmp_path,),
            config=OpenCodeConfig(executable=str(fake), timeout_s=20),
            bwrap=str(tmp_path / "not-bwrap"),
        )

    assert not (source / "new.txt").exists()
    assert _git(source, "status", "--porcelain").strip() == ""
