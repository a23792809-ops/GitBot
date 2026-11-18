"""Per-task OpenCode permission policy generation and verification.

OpenCode's V2 schema uses the ``permissions`` array (``action``, ``resource``,
``effect``; last matching rule wins).  The older singular ``permission`` object
is deprecated, and an array under that key is silently dropped by OpenCode,
which leaves the default (permissive) policy in force.

The Phase 3 spike (``docs/PHASE-3-SPIKE.md`` §5.7) showed that OpenCode fails
open on malformed or unknown config.  Therefore this module never trusts the
file it just wrote: :func:`verify_project_config_loaded` asks
``opencode debug config`` what it actually parsed and fails closed on any
mismatch.  The ``managed_project_config`` context manager also restores any
pre-existing user ``opencode.json`` so GitBot never destroys repository files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gitbot.errors import PolicyViolation
from gitbot.security import redact_text

logger = logging.getLogger("gitbot.opencode.policy")

CONFIG_FILENAME = "opencode.json"
CONFIG_SCHEMA = "https://opencode.ai/config.json"
DEFAULT_DEBUG_TIMEOUT_S = 30.0
GATEWAY_NPM = "@ai-sdk/openai-compatible"

ALLOWED_ACTIONS = frozenset({"shell", "execute", "edit", "write", "read"})
ALLOWED_EFFECTS = frozenset({"allow", "deny", "ask"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class PermissionRule:
    action: str
    resource: str
    effect: str

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "resource": self.resource, "effect": self.effect}


@dataclass(frozen=True, slots=True)
class GatewayPolicy:
    """A loopback-only OpenAI-compatible provider for the sandbox.

    ``api_key`` is a per-run capability token, never a provider credential.  It
    is still treated as secret: it is only written to the disposable sandbox
    config and is never included in audit details or error messages.
    """

    provider_id: str
    model_id: str
    base_url: str
    # Never render the capability token in a repr or traceback.
    api_key: str = field(repr=False)
    name: str = "GitBot Host Gateway"

    def validate(self) -> None:
        if not _IDENTIFIER.fullmatch(self.provider_id):
            raise PolicyViolation("invalid gateway provider identifier")
        if not _IDENTIFIER.fullmatch(self.model_id):
            raise PolicyViolation("invalid gateway model identifier")
        if not self.api_key or len(self.api_key) < 16:
            raise PolicyViolation("gateway capability token is missing or too short")
        try:
            parsed = urlsplit(self.base_url)
        except ValueError as exc:
            raise PolicyViolation("invalid gateway base URL") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/v1"
        ):
            raise PolicyViolation("gateway base URL must be http://127.0.0.1:PORT/v1")

    def provider_document(self) -> dict[str, Any]:
        self.validate()
        return {
            self.provider_id: {
                "npm": GATEWAY_NPM,
                "name": self.name,
                "options": {
                    "baseURL": self.base_url,
                    "apiKey": self.api_key,
                },
                "models": {
                    self.model_id: {
                        "name": "GitBot Gateway",
                        "tools": True,
                        "limit": {"context": 128000, "output": 8192},
                    }
                },
            }
        }

    @property
    def model_reference(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


def _validate_rules(rules: Sequence[PermissionRule]) -> None:
    if not rules:
        raise PolicyViolation("at least one permission rule is required")
    for rule in rules:
        if rule.action not in ALLOWED_ACTIONS:
            raise PolicyViolation(f"unsupported permission action: {rule.action}")
        if rule.effect not in ALLOWED_EFFECTS:
            raise PolicyViolation(f"unsupported permission effect: {rule.effect}")
        if not rule.resource:
            raise PolicyViolation("permission resource cannot be empty")


def render_config(
    rules: Sequence[PermissionRule],
    *,
    gateway: GatewayPolicy | None = None,
) -> dict[str, Any]:
    """Return the V2 config document for ``rules`` (validated before rendering)."""
    _validate_rules(rules)
    payload: dict[str, Any] = {
        "$schema": CONFIG_SCHEMA,
        "permissions": [rule.to_dict() for rule in rules],
    }
    if gateway is not None:
        payload["provider"] = gateway.provider_document()
        payload["model"] = gateway.model_reference
    return payload


def build_project_opencode_config(
    repo_path: Path | str,
    rules: Sequence[PermissionRule],
    *,
    gateway: GatewayPolicy | None = None,
    write: bool = True,
) -> Path:
    """Write (or just locate) the per-task ``opencode.json`` inside ``repo_path``."""
    payload = render_config(rules, gateway=gateway)
    target = Path(repo_path) / CONFIG_FILENAME
    if write:
        # Atomic replace so a crash cannot leave a half-written policy file.
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".opencode-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    return target


def validate_project_config(
    repo_path: Path | str,
    *,
    expected_rules: Sequence[PermissionRule] | None = None,
    expected_gateway: GatewayPolicy | None = None,
) -> dict[str, Any]:
    """Statically validate the config file GitBot intends OpenCode to load."""
    target = Path(repo_path) / CONFIG_FILENAME
    if not target.is_file():
        raise PolicyViolation("missing opencode.json in project directory")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PolicyViolation("opencode.json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PolicyViolation("opencode.json must be an object")
    if "permission" in data:
        raise PolicyViolation(
            "opencode.json uses the deprecated 'permission' key; "
            "'permissions' is required or OpenCode ignores the policy"
        )

    permissions = data.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        raise PolicyViolation("opencode.json must contain a non-empty 'permissions' array")

    for item in permissions:
        if not isinstance(item, dict):
            raise PolicyViolation("permission entries must be objects")
        for key in ("action", "resource", "effect"):
            if key not in item:
                raise PolicyViolation(f"permission entry missing '{key}'")
        if item["action"] not in ALLOWED_ACTIONS:
            raise PolicyViolation(f"unsupported permission action: {item['action']}")
        if item["effect"] not in ALLOWED_EFFECTS:
            raise PolicyViolation(f"unsupported permission effect: {item['effect']}")

    if expected_rules is not None:
        expected = [rule.to_dict() for rule in expected_rules]
        if permissions != expected:
            raise PolicyViolation("opencode.json permissions do not match the requested policy")

    if expected_gateway is not None:
        expected_gateway.validate()
        expected_provider = expected_gateway.provider_document()
        if data.get("provider") != expected_provider:
            raise PolicyViolation(
                "opencode.json gateway provider does not match the requested gateway"
            )
        if data.get("model") != expected_gateway.model_reference:
            raise PolicyViolation(
                "opencode.json gateway model does not match the requested gateway"
            )

    return data


def verify_project_config_loaded(
    repo_path: Path | str,
    *,
    expected_rules: Sequence[PermissionRule] | None = None,
    expected_gateway: GatewayPolicy | None = None,
    executable: str = "opencode",
    timeout_s: float = DEFAULT_DEBUG_TIMEOUT_S,
) -> dict[str, Any]:
    """Ask ``opencode debug config`` what it parsed and fail closed on mismatch.

    A file on disk is not evidence that OpenCode loaded it: the spike showed a
    malformed file is silently ignored and the run proceeds under defaults.
    """
    import subprocess

    target = (Path(repo_path) / CONFIG_FILENAME).resolve()
    argv = [executable, "debug", "config"]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PolicyViolation(
            f"cannot verify OpenCode policy: executable {executable!r} not found",
            details={"executable": executable},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PolicyViolation("timed out while verifying the OpenCode policy") from exc

    if completed.returncode != 0:
        raise PolicyViolation(
            "opencode debug config failed",
            details={
                "exit_code": completed.returncode,
                "stderr": redact_text(completed.stderr or "")[-800:],
            },
        )

    try:
        sources = json.loads(completed.stdout or "null")
    except json.JSONDecodeError as exc:
        raise PolicyViolation("opencode debug config did not return JSON") from exc
    if not isinstance(sources, list):
        raise PolicyViolation("opencode debug config returned an unexpected shape")

    document = None
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "document":
            continue
        try:
            if Path(str(source.get("path", ""))).resolve() == target:
                document = source
                break
        except OSError:  # pragma: no cover - defensive
            continue

    if document is None:
        raise PolicyViolation(
            "OpenCode did not load the GitBot project config",
            details={"path": str(target)},
        )

    info = document.get("info")
    permissions = info.get("permissions") if isinstance(info, dict) else None
    if not isinstance(permissions, list) or not permissions:
        raise PolicyViolation("OpenCode loaded opencode.json but ignored its permissions")

    if expected_rules is not None:
        expected = [rule.to_dict() for rule in expected_rules]
        if permissions != expected:
            raise PolicyViolation("OpenCode loaded different permissions than requested")

    if expected_gateway is not None:
        if not isinstance(info, dict):
            raise PolicyViolation("OpenCode did not load the gateway configuration")
        expected_gateway.validate()
        model = info.get("model")
        expected_model = {
            "providerID": expected_gateway.provider_id,
            "model": expected_gateway.model_id,
        }
        if model != expected_model:
            raise PolicyViolation("OpenCode loaded a different model than the GitBot gateway")
        providers = info.get("providers")
        if not isinstance(providers, dict) or set(providers) != {expected_gateway.provider_id}:
            raise PolicyViolation("OpenCode loaded unexpected providers in the sandbox")
        provider = providers[expected_gateway.provider_id]
        if not isinstance(provider, dict):
            raise PolicyViolation("OpenCode loaded an invalid gateway provider")
        if provider.get("package") != f"aisdk:{GATEWAY_NPM}":
            raise PolicyViolation("OpenCode loaded the wrong gateway provider package")
        settings = provider.get("settings")
        if not isinstance(settings, dict):
            raise PolicyViolation("OpenCode did not load gateway connection settings")
        if settings.get("baseURL") != expected_gateway.base_url:
            raise PolicyViolation("OpenCode loaded the wrong gateway base URL")
        if settings.get("apiKey") not in {None, "***"}:
            raise PolicyViolation("OpenCode exposed an unexpected gateway credential")
        models = provider.get("models")
        if not isinstance(models, dict) or expected_gateway.model_id not in models:
            raise PolicyViolation("OpenCode did not load the GitBot gateway model")

    return document


@contextmanager
def managed_project_config(
    repo_path: Path | str,
    rules: Sequence[PermissionRule],
    *,
    gateway: GatewayPolicy | None = None,
    verify: bool = True,
    executable: str = "opencode",
    timeout_s: float = DEFAULT_DEBUG_TIMEOUT_S,
) -> Iterator[Path]:
    """Install the task policy for the duration of a run and restore the original.

    If a repository already ships an ``opencode.json`` it is backed up and put
    back afterwards, so GitBot never overwrites user configuration or leaves its
    own policy file behind in the reviewed diff.
    """
    target = Path(repo_path) / CONFIG_FILENAME
    original = target.read_bytes() if target.is_file() else None
    try:
        build_project_opencode_config(repo_path, rules, gateway=gateway)
        validate_project_config(repo_path, expected_rules=rules, expected_gateway=gateway)
        if verify:
            verify_project_config_loaded(
                repo_path,
                expected_rules=rules,
                expected_gateway=gateway,
                executable=executable,
                timeout_s=timeout_s,
            )
        yield target
    finally:
        try:
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(original)
        except OSError:  # pragma: no cover - best effort restore
            logger.exception("failed to restore %s", target)
