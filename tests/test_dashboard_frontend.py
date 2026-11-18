"""Run the dashboard JavaScript behaviour tests as part of the Python suite.

The dashboard is a static HTML file with inline JavaScript, so its form logic
cannot be exercised by pytest directly.  These tests execute the real script
under Node's built-in test runner against a small DOM/fetch harness.  Node is
not a runtime dependency of GitBot, so the test is skipped when it is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required for the dashboard JavaScript tests")
def test_dashboard_task_form_behaviour() -> None:
    result = subprocess.run(
        [NODE, "--test", "tests/frontend/dashboard_form.test.mjs"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"dashboard JavaScript tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
