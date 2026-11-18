"""Tests for the OpenCode permission policy generation and verification."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from gitbot.errors import PolicyViolation
from gitbot.opencode.policy import (
    GATEWAY_NPM,
    GatewayPolicy,
    PermissionRule,
    build_project_opencode_config,
    managed_project_config,
    render_config,
    validate_project_config,
    verify_project_config_loaded,
)

RULES = [
    PermissionRule("shell", "git status *", "allow"),
    PermissionRule("shell", "*", "deny"),
    PermissionRule("execute", "*", "deny"),
]
GATEWAY = GatewayPolicy(
    provider_id="gitbot-gateway",
    model_id="gitbot",
    base_url="http://127.0.0.1:18080/v1",
    api_key="capability-token-0123456789",
)


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_opencode(tmp_path: Path, *, include_permissions: bool = True) -> Path:
    body = f"""
import json, os, sys
info = {{"$schema": "https://opencode.ai/config.json"}}
if {include_permissions!r}:
    with open(os.path.join(os.getcwd(), "opencode.json"), encoding="utf-8") as handle:
        info = json.load(handle)
document = {{"type": "document", "path": os.path.join(os.getcwd(), "opencode.json")}}
document["info"] = info
print(json.dumps([document]))
"""
    return _write_executable(tmp_path / "opencode", body)


def test_render_uses_v2_permissions_key() -> None:
    payload = render_config(RULES)
    assert "permissions" in payload
    assert "permission" not in payload
    assert payload["permissions"][0] == {
        "action": "shell",
        "resource": "git status *",
        "effect": "allow",
    }


def test_render_rejects_unknown_effect() -> None:
    with pytest.raises(PolicyViolation):
        render_config([PermissionRule("shell", "*", "deny_typo")])


def test_validate_rejects_deprecated_permission_key(tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": [rule.to_dict() for rule in RULES]}), encoding="utf-8"
    )
    with pytest.raises(PolicyViolation, match="deprecated"):
        validate_project_config(tmp_path)


def test_validate_rejects_policy_mismatch(tmp_path: Path) -> None:
    build_project_opencode_config(tmp_path, RULES)
    with pytest.raises(PolicyViolation, match="do not match"):
        validate_project_config(tmp_path, expected_rules=[PermissionRule("shell", "*", "deny")])


def test_verify_accepts_loaded_permissions(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path)
    build_project_opencode_config(tmp_path, RULES)
    document = verify_project_config_loaded(tmp_path, expected_rules=RULES, executable=str(fake))
    assert document["info"]["permissions"] == [rule.to_dict() for rule in RULES]


def test_verify_fails_closed_when_permissions_ignored(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path, include_permissions=False)
    build_project_opencode_config(tmp_path, RULES)
    with pytest.raises(PolicyViolation, match="ignored"):
        verify_project_config_loaded(tmp_path, expected_rules=RULES, executable=str(fake))


def test_verify_fails_closed_when_document_missing(tmp_path: Path) -> None:
    fake = _write_executable(tmp_path / "opencode", 'print("[]")\n')
    build_project_opencode_config(tmp_path, RULES)
    with pytest.raises(PolicyViolation, match="did not load"):
        verify_project_config_loaded(tmp_path, executable=str(fake))


def test_managed_config_removes_its_own_file(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path)
    with managed_project_config(tmp_path, RULES, executable=str(fake)):
        assert (tmp_path / "opencode.json").is_file()
    assert not (tmp_path / "opencode.json").exists()


def test_managed_config_restores_user_file(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path)
    original = '{\n  "$schema": "https://opencode.ai/config.json"\n}\n'
    (tmp_path / "opencode.json").write_text(original, encoding="utf-8")
    with managed_project_config(tmp_path, RULES, executable=str(fake)):
        assert "permissions" in (tmp_path / "opencode.json").read_text(encoding="utf-8")
    assert (tmp_path / "opencode.json").read_text(encoding="utf-8") == original


def _fake_gateway_opencode(
    tmp_path: Path,
    *,
    leak_key: bool = False,
    wrong_model: bool = False,
    extra_provider: bool = False,
) -> Path:
    body = f"""
import json, os
with open(os.path.join(os.getcwd(), "opencode.json"), encoding="utf-8") as handle:
    config = json.load(handle)
provider_id, provider = next(iter(config["provider"].items()))
model_id = config["model"].split("/", 1)[1]
if {wrong_model!r}:
    model_id = "other"
settings = {{
    "baseURL": provider["options"]["baseURL"],
    "apiKey": provider["options"]["apiKey"] if {leak_key!r} else "***",
}}
providers = {{
    provider_id: {{
        "package": "aisdk:" + provider["npm"],
        "settings": settings,
        "models": provider["models"],
    }}
}}
if {extra_provider!r}:
    providers["unexpected"] = providers[provider_id]
info = {{
    "permissions": config["permissions"],
    "model": {{"providerID": provider_id, "model": model_id}},
    "providers": providers,
}}
path = os.path.join(os.getcwd(), "opencode.json")
print(json.dumps([{{"type": "document", "path": path, "info": info}}]))
"""
    return _write_executable(tmp_path / "opencode-gateway", body)


def test_render_includes_a_loopback_gateway_provider() -> None:
    payload = render_config(RULES, gateway=GATEWAY)

    assert payload["model"] == "gitbot-gateway/gitbot"
    provider = payload["provider"]["gitbot-gateway"]
    assert provider["npm"] == GATEWAY_NPM
    assert provider["options"] == {
        "baseURL": "http://127.0.0.1:18080/v1",
        "apiKey": GATEWAY.api_key,
    }
    assert provider["models"]["gitbot"]["tools"] is True


def test_gateway_policy_rejects_unsafe_identifiers_and_urls() -> None:
    with pytest.raises(PolicyViolation, match="provider identifier"):
        GatewayPolicy("Bad Provider", "gitbot", GATEWAY.base_url, GATEWAY.api_key).validate()
    with pytest.raises(PolicyViolation, match="model identifier"):
        GatewayPolicy(
            GATEWAY.provider_id,
            "bad/model",
            GATEWAY.base_url,
            GATEWAY.api_key,
        ).validate()
    with pytest.raises(PolicyViolation, match="base URL"):
        GatewayPolicy(
            GATEWAY.provider_id,
            GATEWAY.model_id,
            "http://0.0.0.0:1/v1",
            GATEWAY.api_key,
        ).validate()


def test_validate_detects_a_different_gateway(tmp_path: Path) -> None:
    build_project_opencode_config(tmp_path, RULES, gateway=GATEWAY)
    other = GatewayPolicy(
        provider_id=GATEWAY.provider_id,
        model_id=GATEWAY.model_id,
        base_url=GATEWAY.base_url,
        api_key="different-capability-token",
    )
    with pytest.raises(PolicyViolation, match="gateway provider"):
        validate_project_config(tmp_path, expected_gateway=other)


def test_verify_accepts_the_loaded_gateway_provider(tmp_path: Path) -> None:
    fake = _fake_gateway_opencode(tmp_path)
    build_project_opencode_config(tmp_path, RULES, gateway=GATEWAY)

    document = verify_project_config_loaded(
        tmp_path,
        expected_rules=RULES,
        expected_gateway=GATEWAY,
        executable=str(fake),
    )

    assert document["info"]["model"] == {
        "providerID": GATEWAY.provider_id,
        "model": GATEWAY.model_id,
    }
    assert document["info"]["providers"][GATEWAY.provider_id]["settings"]["apiKey"] == "***"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"leak_key": True}, "unexpected gateway credential"),
        ({"wrong_model": True}, "different model"),
        ({"extra_provider": True}, "unexpected providers"),
    ],
)
def test_verify_fails_closed_on_gateway_mismatch(
    tmp_path: Path,
    options: dict[str, bool],
    message: str,
) -> None:
    fake = _fake_gateway_opencode(tmp_path, **options)
    build_project_opencode_config(tmp_path, RULES, gateway=GATEWAY)

    with pytest.raises(PolicyViolation, match=message):
        verify_project_config_loaded(
            tmp_path,
            expected_rules=RULES,
            expected_gateway=GATEWAY,
            executable=str(fake),
        )


def test_managed_config_with_gateway_restores_and_removes_the_token(tmp_path: Path) -> None:
    fake = _fake_gateway_opencode(tmp_path)
    with managed_project_config(
        tmp_path,
        RULES,
        gateway=GATEWAY,
        executable=str(fake),
    ) as target:
        assert GATEWAY.api_key in target.read_text(encoding="utf-8")
    assert not (tmp_path / "opencode.json").exists()
