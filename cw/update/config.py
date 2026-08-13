from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cw.core.errors import CwError, ErrorCode
from cw.core.utils import atomic_write
from cw.core.toml import load_toml


@dataclass(frozen=True, slots=True)
class UpdateSettings:
    channel: str = "stable"
    check: bool = True
    check_interval_hours: int = 24


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cw"


def update_cache_path() -> Path:
    return config_dir() / "update.json"


def global_config_path() -> Path:
    return config_dir() / "config.toml"


def load_global_document() -> dict[str, Any]:
    path = global_config_path()
    if not path.is_file():
        return {}
    try:
        value = load_toml(path)
    except Exception as exc:
        raise CwError("Global configuration is invalid TOML", ErrorCode.USAGE_ERROR, details=str(exc), exit_code=2) from exc
    return value if isinstance(value, dict) else {}


def load_update_settings() -> UpdateSettings:
    source = load_global_document().get("updates", {})
    if not isinstance(source, dict):
        raise CwError("[updates] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    unknown = set(source) - {"channel", "check", "check_interval_hours"}
    if unknown:
        raise CwError(f"Unknown update setting: {', '.join(sorted(unknown))}", ErrorCode.USAGE_ERROR, exit_code=2)
    channel = os.environ.get("CW_UPDATE_CHANNEL", source.get("channel", "stable"))
    if channel not in {"stable", "beta", "dev"}:
        raise CwError("Update channel must be stable, beta, or dev", ErrorCode.USAGE_ERROR, exit_code=2)
    enabled = source.get("check", True)
    interval = source.get("check_interval_hours", 24)
    if not isinstance(enabled, bool) or isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise CwError("Update check settings are invalid", ErrorCode.USAGE_ERROR, exit_code=2)
    if os.environ.get("CW_NO_UPDATE_CHECK", "").lower() in {"1", "true", "yes"}:
        enabled = False
    return UpdateSettings(channel=channel, check=enabled, check_interval_hours=interval)


def set_update_setting(key: str, raw: str) -> tuple[Any, UpdateSettings]:
    if key not in {"updates.channel", "updates.check", "updates.check_interval_hours"}:
        raise CwError(f"Unknown update setting: {key}", ErrorCode.USAGE_ERROR, exit_code=2)
    document = load_global_document()
    updates = document.setdefault("updates", {})
    if not isinstance(updates, dict):
        raise CwError("[updates] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    leaf = key.split(".", 1)[1]
    if leaf == "channel":
        value: Any = raw.lower()
        if value not in {"stable", "beta", "dev"}:
            raise CwError("Update channel must be stable, beta, or dev", ErrorCode.USAGE_ERROR, exit_code=2)
    elif leaf == "check":
        if raw.lower() not in {"true", "false"}:
            raise CwError("updates.check must be true or false", ErrorCode.USAGE_ERROR, exit_code=2)
        value = raw.lower() == "true"
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise CwError("updates.check_interval_hours must be a positive integer", ErrorCode.USAGE_ERROR, exit_code=2) from exc
        if value < 1:
            raise CwError("updates.check_interval_hours must be a positive integer", ErrorCode.USAGE_ERROR, exit_code=2)
    updates[leaf] = value
    write_global_document(document)
    return value, load_update_settings()


def write_global_document(document: dict[str, Any]) -> None:
    atomic_write(global_config_path(), _render_global_toml(document))


def _render_global_toml(document: dict[str, Any]) -> str:
    lines = ["# CW global preferences"]
    ordinary = {key: value for key, value in document.items() if not isinstance(value, dict)}
    for key, value in ordinary.items():
        lines.append(f"{key} = {_toml_value(value)}")
    ordered_sections = {
        "updates": ("channel", "check", "check_interval_hours"),
        "execution": (
            "default_phases", "recommended_max_phases", "hard_max_phases",
            "default_max_time", "max_semantic_revisions_per_phase",
        ),
        "observability": ("heartbeat_seconds", "quiet_threshold_seconds"),
    }
    for section, keys in ordered_sections.items():
        values = document.get(section, {})
        if values:
            lines.extend(["", f"[{section}]"])
            for key in keys:
                if key in values:
                    lines.append(f"{key} = {_toml_value(values[key])}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
