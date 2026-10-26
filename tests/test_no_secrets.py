"""Guard tests: no credential-shaped strings may enter the repository.

These patterns are deliberately synthetic and match only *shapes* of secrets.
They exist so that a future accidental paste fails CI/tests instead of leaking.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
}

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".whl", ".zip"}

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "github_classic_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "github_fine_grained_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bearer_header": re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}"),
}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def test_no_credential_shaped_strings_in_repo() -> None:
    findings: list[str] = []
    for path in _iter_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file is a real failure
            findings.append(f"{path}: unreadable")
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(REPO_ROOT)}: {name}")

    assert not findings, "credential-shaped strings found: " + "; ".join(findings)


def test_env_file_is_ignored_but_env_example_is_tracked() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip() for line in gitignore if line.strip()}

    assert ".env" in entries
    assert ".env.*" in entries
    assert "!.env.example" in entries
    assert (REPO_ROOT / ".env.example").is_file()


def test_env_example_contains_only_placeholder_values() -> None:
    content = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        assert key and value, f"empty value in .env.example: {stripped}"
        lowered = value.lower()
        assert "token=" not in lowered, f"looks like an inline secret: {key}"
        assert not value.startswith(("ghp_", "gho_", "github_pat_")), f"secret in {key}"
