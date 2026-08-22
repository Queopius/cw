from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from cw import __version__
from cw.application import ApplicationError, ApplicationErrorCode
from cw.update.models import Version


POLICY_RESOURCE = "plugin-compatibility.json"


def _failure(message: str, *, details: dict[str, Any] | None = None) -> ApplicationError:
    return ApplicationError(
        ApplicationErrorCode.PLATFORM_CAPABILITY_UNAVAILABLE,
        message,
        details={"action": "Install a CW Core version allowed by the Plugin compatibility policy", **(details or {})},
    )


def load_plugin_compatibility(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8") if path is not None else (
            files("cw.adapters.mcp").joinpath(POLICY_RESOURCE).read_text(encoding="utf-8")
        )
        policy = json.loads(raw)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise _failure("CW Plugin compatibility policy could not be loaded") from exc
    expected = {"schema_version", "policy_id", "plugin_version", "core", "remote_protocol"}
    if not isinstance(policy, dict) or set(policy) != expected:
        raise _failure("CW Plugin compatibility policy is invalid")
    core = policy.get("core")
    if (
        policy.get("schema_version") != 1
        or policy.get("policy_id") != "cw.plugin.compatibility.v1"
        or policy.get("plugin_version") != "0.1.0"
        or policy.get("remote_protocol") != "cw.remote.v1"
        or not isinstance(core, dict)
        or set(core) != {"minimum", "maximum_exclusive"}
        or not all(isinstance(core.get(key), str) for key in core)
    ):
        raise _failure("CW Plugin compatibility policy is invalid")
    try:
        minimum = Version.parse(core["minimum"])
        maximum = Version.parse(core["maximum_exclusive"])
    except Exception as exc:
        raise _failure("CW Plugin compatibility policy contains invalid versions") from exc
    if not minimum < maximum:
        raise _failure("CW Plugin compatibility policy contains an invalid range")
    return policy


def ensure_plugin_compatible(
    *, core_version: str | None = None, plugin_version: str | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    policy = load_plugin_compatibility(policy_path)
    installed_value = __version__ if core_version is None else core_version
    try:
        installed = Version.parse(installed_value)
        minimum = Version.parse(policy["core"]["minimum"])
        maximum = Version.parse(policy["core"]["maximum_exclusive"])
    except Exception as exc:
        raise _failure(
            "Installed CW Core version cannot be verified",
            details={"installed_core": installed_value},
        ) from exc
    if installed < minimum or not installed < maximum:
        raise _failure(
            f"Installed CW Core version is incompatible with CW Plugin {policy['plugin_version']}",
            details={
                "installed_core": str(installed),
                "minimum_core": str(minimum),
                "maximum_core_exclusive": str(maximum),
                "plugin_version": policy["plugin_version"],
            },
        )
    if plugin_version is not None and plugin_version != policy["plugin_version"]:
        raise _failure(
            "Configured CW Plugin version does not match the runtime compatibility policy",
            details={
                "configured_plugin": plugin_version,
                "required_plugin": policy["plugin_version"],
            },
        )
    return policy
