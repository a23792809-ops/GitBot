"""Redaction tests.

Credential-shaped fixtures are assembled at runtime so this file itself never
contains a literal that would trip tests/test_no_secrets.py.
"""

from __future__ import annotations

import logging

import pytest

from gitbot.security import RedactionFilter, contains_secret, redact_text, redact_value

GH_TOKEN = "gh" + "p_" + ("a1B2c3D4" * 5)
GH_PAT = "github" + "_pat_" + ("A1b2C3" * 5)
AWS_KEY = "AKIA" + "Q" * 16
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
PEM_FOOTER = "-----END " + "RSA PRIVATE KEY" + "-----"
OR_KEY = "sk" + "-or-v1-" + ("c3d4e5f6" * 3)
OAI_KEY = "sk" + "-proj-" + ("A1b2C3d4" * 4)
ANT_KEY = "sk" + "-ant-api03-" + ("Q7w8E9r0" * 3)


def test_github_token_is_redacted() -> None:
    result = redact_text(f"using {GH_TOKEN} for auth")
    assert GH_TOKEN not in result
    assert "[REDACTED:github_token]" in result
    assert result.startswith("using ")


def test_fine_grained_pat_is_redacted() -> None:
    result = redact_text(f"pat={GH_PAT}")
    assert GH_PAT not in result
    assert "REDACTED" in result


def test_aws_key_is_redacted() -> None:
    result = redact_text(AWS_KEY)
    assert result == "[REDACTED:aws_access_key]"


def test_private_key_block_is_fully_removed() -> None:
    block = f"{PEM_HEADER}\nMIIEowIBAAKCAQEA\n{PEM_FOOTER}"
    result = redact_text(f"key follows:\n{block}\ntrailing")
    assert "MIIEowIBAAKCAQEA" not in result
    assert PEM_HEADER not in result
    assert "trailing" in result
    assert "[REDACTED:private_key]" in result


def test_bearer_header_keeps_scheme_drops_credential() -> None:
    result = redact_text("Authorization: Bearer abcdef1234567890XYZ")
    assert "abcdef1234567890XYZ" not in result
    assert "Authorization: Bearer" in result
    assert "[REDACTED:bearer]" in result


def test_generic_key_value_secret_is_redacted() -> None:
    result = redact_text("client_secret=super-secret-value-123")
    assert "super-secret-value-123" not in result


def test_openai_style_provider_key_is_redacted() -> None:
    for key in (OR_KEY, OAI_KEY, ANT_KEY):
        result = redact_text(f"provider rejected {key} for auth")
        assert key not in result
        assert "[REDACTED:openai_style_key]" in result


def test_openai_style_key_inside_error_sentence_is_redacted() -> None:
    # A provider echo without a keyword in front of the key must still be caught.
    result = redact_text(f"Invalid API key {OR_KEY} supplied")
    assert OR_KEY not in result


def test_ordinary_text_is_untouched() -> None:
    text = "Fixed 3 tests; ruff clean; commit e60612f on main."
    assert redact_text(text) == text
    assert not contains_secret(text)


def test_contains_secret_positive() -> None:
    assert contains_secret(f"token {GH_TOKEN}")


def test_redaction_is_idempotent() -> None:
    once = redact_text(f"x {GH_TOKEN} y")
    assert redact_text(once) == once


def test_redact_value_handles_nested_structures() -> None:
    payload = {
        "token": GH_TOKEN,
        "nested": [{"note": f"leak {AWS_KEY}"}, 42, True, None],
        "count": 3,
    }
    cleaned = redact_value(payload)

    assert cleaned["count"] == 3
    assert cleaned["nested"][1:] == [42, True, None]
    serialized = repr(cleaned)
    assert GH_TOKEN not in serialized
    assert AWS_KEY not in serialized


def test_redaction_filter_scrubs_log_records() -> None:
    record = logging.LogRecord(
        name="gitbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth with %s",
        args=(GH_TOKEN,),
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    assert GH_TOKEN not in record.getMessage()


@pytest.mark.parametrize(
    "value",
    ["", "ghp_short", "not-a-secret", "secret="],
)
def test_no_false_positive_on_short_values(value: str) -> None:
    assert redact_text(value) == value


def test_redact_value_preserves_container_types() -> None:
    assert redact_value(("keep", 2)) == ("keep", 2)
    assert isinstance(redact_value(("a",)), tuple)
    assert isinstance(redact_value(["a"]), list)


def test_filter_keeps_positional_formatting_working() -> None:
    """Regression: converting args to a list broke `"%d" % args` in uvicorn logs."""
    record = logging.LogRecord(
        name="uvicorn.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Finished server process [%d] with %s",
        args=(4242, "ok"),
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    assert record.getMessage() == "Finished server process [4242] with ok"


def test_filter_redacts_within_positional_args() -> None:
    record = logging.LogRecord(
        name="gitbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth %s",
        args=(GH_TOKEN,),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    assert record.getMessage() == "auth [REDACTED:github_token]"


def test_filter_handles_mapping_args() -> None:
    record = logging.LogRecord(
        name="gitbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=%(token)s",
        args=({"token": GH_TOKEN},),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    assert GH_TOKEN not in record.getMessage()
