"""Read-only git adapter tests against real throwaway repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitbot.gitops.repo import (
    GitCommandError,
    GitRepo,
    NotAGitRepository,
    ensure_git_repository,
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
    _git(path, "add", "app.py")
    _git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(_init_repo(tmp_path / "repo"))


def test_clean_repository_status(repo: GitRepo) -> None:
    status = repo.status()

    assert status.branch == "main"
    assert status.clean is True
    assert status.changed_files == ()
    assert status.detached is False
    assert status.head is not None
    assert len(status.head) == 40


def test_untracked_and_modified_files_are_reported(repo: GitRepo) -> None:
    (repo.path / "new.txt").write_text("hello", encoding="utf-8")
    (repo.path / "app.py").write_text("print('two')\n", encoding="utf-8")

    status = repo.status()
    assert status.clean is False
    assert "new.txt" in status.changed_files
    assert "app.py" in status.changed_files


def test_working_tree_diff(repo: GitRepo) -> None:
    (repo.path / "app.py").write_text("print('two')\n", encoding="utf-8")

    diff = repo.diff()
    assert diff.files == ("app.py",)
    assert "print('two')" in diff.patch
    assert diff.truncated is False
    assert diff.staged is False


def test_staged_diff_only_after_add(repo: GitRepo) -> None:
    (repo.path / "app.py").write_text("print('staged')\n", encoding="utf-8")
    assert repo.diff(staged=True).patch == ""

    _git(repo.path, "add", "app.py")
    staged = repo.diff(staged=True)
    assert "print('staged')" in staged.patch
    assert staged.files == ("app.py",)


def test_diff_against_base(repo: GitRepo) -> None:
    (repo.path / "app.py").write_text("print('second')\n", encoding="utf-8")
    _git(repo.path, "commit", "-q", "-am", "second")

    diff = repo.diff(base="HEAD~1")
    assert "print('second')" in diff.patch


def test_diff_is_truncated_at_the_limit(repo: GitRepo) -> None:
    (repo.path / "app.py").write_text("x = 1\n" * 500, encoding="utf-8")

    diff = repo.diff(max_chars=64)
    assert diff.truncated is True
    assert len(diff.patch) == 64


def test_preflight_passes_on_clean_repo(repo: GitRepo) -> None:
    preflight = repo.preflight("task-1")

    assert preflight.ok is True
    assert preflight.reasons == ()
    assert preflight.suggested_branch == "gitbot/task-1"


def test_preflight_flags_dirty_tree(repo: GitRepo) -> None:
    (repo.path / "app.py").write_text("dirty\n", encoding="utf-8")

    preflight = repo.preflight("task-2")
    assert preflight.ok is False
    assert "working tree has uncommitted changes" in preflight.reasons


def test_preflight_flags_detached_head(repo: GitRepo) -> None:
    _git(repo.path, "checkout", "-q", "--detach", "HEAD")

    preflight = repo.preflight("task-3")
    assert preflight.ok is False
    assert any("detached" in reason for reason in preflight.reasons)


def test_preflight_flags_existing_task_branch(repo: GitRepo) -> None:
    _git(repo.path, "branch", "gitbot/task-4")

    preflight = repo.preflight("task-4")
    assert preflight.ok is False
    assert any("already exists" in reason for reason in preflight.reasons)


def test_branch_exists(repo: GitRepo) -> None:
    assert repo.branch_exists("main") is True
    assert repo.branch_exists("nope") is False


def test_non_repository_raises_git_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(GitCommandError) as excinfo:
        GitRepo(plain).status()
    assert "fatal" in str(excinfo.value).lower() or excinfo.value.details["exit_code"] != 0


def test_ensure_git_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(NotAGitRepository):
        ensure_git_repository(plain)

    repo = _init_repo(tmp_path / "real")
    ensure_git_repository(repo)  # must not raise


def test_missing_git_executable_is_reported(tmp_path: Path) -> None:
    _init_repo(tmp_path / "repo")
    with pytest.raises(GitCommandError, match="not found on PATH"):
        GitRepo(tmp_path / "repo", executable="/nonexistent/git").status()
