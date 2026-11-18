"""Allowlist path validation: containment, symlinks, and self-protection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitbot.security.allowlist import AllowlistError, resolve_repo_path


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def test_valid_repo_inside_allowed_root_is_accepted(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "work")
    resolved = resolve_repo_path(repo, allowed_roots=[tmp_path])
    assert resolved == repo.resolve()


def test_git_file_target_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8")

    with pytest.raises(AllowlistError, match="git metadata target does not exist"):
        resolve_repo_path(repo, allowed_roots=[tmp_path])


def test_symlinked_git_metadata_is_rejected(tmp_path: Path) -> None:
    main = _git_repo(tmp_path / "main")
    repo = tmp_path / "linked"
    repo.mkdir()
    (repo / ".git").symlink_to(main / ".git")

    with pytest.raises(AllowlistError, match="must not be a symlink"):
        resolve_repo_path(repo, allowed_roots=[tmp_path])


def test_worktree_with_git_metadata_inside_allowed_root_is_accepted(tmp_path: Path) -> None:
    main = _git_repo(tmp_path / "main")
    subprocess.run(
        ["git", "-C", str(main), "config", "user.email", "test@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main), "config", "user.name", "GitBot Test"],
        check=True,
        capture_output=True,
    )
    (main / "file.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "-m", "init"], check=True, capture_output=True
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(worktree)],
        check=True,
        capture_output=True,
    )

    assert resolve_repo_path(worktree, allowed_roots=[tmp_path]) == worktree.resolve()


def test_non_git_directory_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(AllowlistError, match="not a git repository"):
        resolve_repo_path(plain, allowed_roots=[tmp_path])


def test_missing_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError, match="does not exist"):
        resolve_repo_path(tmp_path / "ghost", allowed_roots=[tmp_path])


def test_file_is_rejected(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(AllowlistError, match="not a directory"):
        resolve_repo_path(file_path, allowed_roots=[tmp_path])


def test_relative_path_is_rejected(tmp_path: Path) -> None:
    _git_repo(tmp_path / "work")
    with pytest.raises(AllowlistError, match="must be absolute"):
        resolve_repo_path("work", allowed_roots=[tmp_path])


def test_empty_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError, match="empty"):
        resolve_repo_path("   ", allowed_roots=[tmp_path])


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = _git_repo(tmp_path.parent / f"{tmp_path.name}-outside")
    link = tmp_path / "escape"
    link.symlink_to(outside)

    try:
        with pytest.raises(AllowlistError, match="outside the allowed roots"):
            resolve_repo_path(link, allowed_roots=[tmp_path])
    finally:
        link.unlink(missing_ok=True)


def test_path_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    inside = _git_repo(tmp_path / "home-repo")
    with pytest.raises(AllowlistError, match="outside the allowed roots"):
        resolve_repo_path(inside, allowed_roots=[tmp_path / "another-root"])


def test_gitbot_source_tree_is_forbidden(tmp_path: Path) -> None:
    project = _git_repo(tmp_path / "GitBot")
    nested = _git_repo(project / "sub" / "pkg")

    with pytest.raises(AllowlistError, match="own source tree"):
        resolve_repo_path(project, allowed_roots=[tmp_path], forbidden_roots=[project])

    with pytest.raises(AllowlistError, match="own source tree"):
        resolve_repo_path(nested, allowed_roots=[tmp_path], forbidden_roots=[project])


def test_tilde_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _git_repo(tmp_path / "tilde-repo")

    resolved = resolve_repo_path("~/tilde-repo", allowed_roots=[tmp_path])
    assert resolved == repo.resolve()


def test_policy_error_carries_stable_code(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(AllowlistError) as excinfo:
        resolve_repo_path(plain, allowed_roots=[tmp_path])
    assert excinfo.value.code == "path_not_allowed"
    assert excinfo.value.http_status == 403


def test_repository_filter_attributes_are_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "filtered")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "notes.txt").write_text("content\n", encoding="utf-8")

    with pytest.raises(AllowlistError, match="filter driver"):
        resolve_repo_path(repo, allowed_roots=[tmp_path])


def test_repository_attributes_without_filter_are_accepted(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "textual")
    (repo / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
    (repo / "notes.txt").write_text("content\n", encoding="utf-8")

    assert resolve_repo_path(repo, allowed_roots=[tmp_path]) == repo.resolve()


def test_gitattributes_filter_without_a_matching_file_is_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "future-filter")
    (repo / ".gitattributes").write_text("*.future filter=evil\n", encoding="utf-8")

    with pytest.raises(AllowlistError, match="filter driver"):
        resolve_repo_path(repo, allowed_roots=[tmp_path])


def test_git_info_attributes_filter_is_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "info-filter")
    info = repo / ".git" / "info" / "attributes"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("* filter=evil\n", encoding="utf-8")

    with pytest.raises(AllowlistError, match="filter driver"):
        resolve_repo_path(repo, allowed_roots=[tmp_path])
