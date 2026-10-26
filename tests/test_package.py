"""Phase 0 smoke tests: the package exists, imports, and reports a version."""

from __future__ import annotations

import gitbot


def test_version_is_a_string() -> None:
    assert isinstance(gitbot.__version__, str)
    assert gitbot.__version__


def test_version_is_phase0_value() -> None:
    assert gitbot.__version__ == "0.1.0"


def test_dunder_all_is_consistent() -> None:
    assert "__version__" in gitbot.__all__
    for name in gitbot.__all__:
        assert hasattr(gitbot, name)
