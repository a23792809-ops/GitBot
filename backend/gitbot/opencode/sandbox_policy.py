"""Install and prove OpenCode policies inside the disposable sandbox.

The project config is written into a copy that the sandbox can see, never into
the user's repository. Verification runs ``opencode debug config`` *inside*
Bubblewrap as well, so a file that exists on the host is never treated as proof
that the model actually loaded it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from gitbot.errors import PolicyViolation
from gitbot.opencode.adapter import OpenCodeConfig
from gitbot.opencode.policy import (
    CONFIG_FILENAME,
    GATEWAY_NPM,
    GatewayPolicy,
    PermissionRule,
    render_config,
)
from gitbot.security import redact_text
from gitbot.security.sandbox import (
    SANDBOX_REPO,
    build_sandbox_command,
    run_sandbox_command,
)

POLICY_VERIFY_TIMEOUT_S = 30.0


def install_policy(
    root: Path | str,
    rules: Sequence[PermissionRule],
    *,
    gateway: GatewayPolicy | None = None,
) -> bytes | None:
    """Write the policy into a disposable tree and return any original bytes.

    The caller must restore the original (or delete the file) before computing
    a review diff, otherwise GitBot's own policy could leak into the patch.
    """
    target = Path(root) / CONFIG_FILENAME
    original = target.read_bytes() if target.is_file() else None
    payload = json.dumps(render_config(rules, gateway=gateway), indent=2) + "\n"
    try:
        if target.is_file():
            os.chmod(target, 0o600)
        target.write_text(payload, encoding="utf-8")
        os.chmod(target, 0o444)
    except OSError as exc:
        raise PolicyViolation("could not install the sandbox policy") from exc
    return original


def restore_policy(root: Path | str, original: bytes | None) -> None:
    """Remove GitBot's policy, restoring the user's original file if there was one."""
    target = Path(root) / CONFIG_FILENAME
    try:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            if target.is_file():
                os.chmod(target, 0o600)
            target.write_bytes(original)
            os.chmod(target, 0o444)
    except OSError as exc:  # pragma: no cover - best effort in a disposable tree
        raise PolicyViolation("could not restore the sandbox policy file") from exc


def _verify_gateway_info(info: Mapping[str, object], gateway: GatewayPolicy) -> None:
    gateway.validate()
    expected_model = {
        "providerID": gateway.provider_id,
        "model": gateway.model_id,
    }
    if info.get("model") != expected_model:
        raise PolicyViolation("OpenCode loaded a different model than the GitBot gateway")
    providers = info.get("providers")
    if not isinstance(providers, dict) or set(providers) != {gateway.provider_id}:
        raise PolicyViolation("OpenCode loaded unexpected providers in the sandbox")
    provider = providers[gateway.provider_id]
    if not isinstance(provider, dict):
        raise PolicyViolation("OpenCode loaded an invalid gateway provider")
    if provider.get("package") != f"aisdk:{GATEWAY_NPM}":
        raise PolicyViolation("OpenCode loaded the wrong gateway provider package")
    settings = provider.get("settings")
    if not isinstance(settings, dict):
        raise PolicyViolation("OpenCode did not load gateway connection settings")
    if settings.get("baseURL") != gateway.base_url:
        raise PolicyViolation("OpenCode loaded the wrong gateway base URL")
    if settings.get("apiKey") not in {None, "***"}:
        raise PolicyViolation("OpenCode exposed an unexpected gateway credential")
    models = provider.get("models")
    if not isinstance(models, dict) or gateway.model_id not in models:
        raise PolicyViolation("OpenCode did not load the GitBot gateway model")


def verify_policy(
    snapshot_root: Path | str,
    workspace_root: Path | str,
    config: OpenCodeConfig,
    *,
    rules: Sequence[PermissionRule],
    gateway: GatewayPolicy | None = None,
    sandbox_cwd: str = SANDBOX_REPO,
    bwrap: str = "bwrap",
) -> None:
    """Prove inside the sandbox that OpenCode loaded exactly the intended rules."""
    spec = build_sandbox_command(
        snapshot_root,
        workspace_root,
        [config.executable, "debug", "config"],
        bwrap=bwrap,
        cwd=sandbox_cwd,
    )
    verified = run_sandbox_command(spec, timeout_s=POLICY_VERIFY_TIMEOUT_S)
    if verified.timed_out:
        raise PolicyViolation("timed out while verifying the sandboxed OpenCode policy")
    if verified.exit_code != 0:
        raise PolicyViolation(
            "sandboxed 'opencode debug config' failed",
            details={
                "exit_code": verified.exit_code,
                "stderr": redact_text(verified.stderr)[-800:],
            },
        )
    try:
        sources = json.loads(verified.stdout or "null")
    except json.JSONDecodeError as exc:
        raise PolicyViolation("sandboxed 'opencode debug config' did not return JSON") from exc
    if not isinstance(sources, list):
        raise PolicyViolation("sandboxed 'opencode debug config' returned an unexpected shape")

    expected_path = PurePosixPath(sandbox_cwd) / CONFIG_FILENAME
    document: Mapping[str, object] | None = None
    for source in sources:
        if not isinstance(source, dict):
            raise PolicyViolation("unexpected configuration source from OpenCode")
        source_type = source.get("type")
        path = str(source.get("path", ""))
        if source_type == "directory":
            # Search directories carry no content of their own.
            continue
        if source_type == "document" and PurePosixPath(path) == expected_path:
            if document is not None:
                raise PolicyViolation("OpenCode reported the sandbox policy more than once")
            document = source
            continue
        raise PolicyViolation(
            "OpenCode loaded an unexpected configuration source",
            details={"type": str(source_type), "path": path},
        )

    if document is None:
        raise PolicyViolation(
            "OpenCode did not load the sandboxed policy",
            details={"path": str(expected_path)},
        )
    info = document.get("info")
    permissions = info.get("permissions") if isinstance(info, dict) else None
    expected = [rule.to_dict() for rule in rules]
    if not isinstance(permissions, list) or not permissions:
        raise PolicyViolation("OpenCode ignored the sandbox permissions")
    if permissions != expected:
        raise PolicyViolation(
            "OpenCode loaded different permissions than requested",
            details={"expected": expected, "actual": permissions},
        )
    if gateway is not None:
        if not isinstance(info, dict):
            raise PolicyViolation("OpenCode did not load the gateway configuration")
        _verify_gateway_info(info, gateway)
