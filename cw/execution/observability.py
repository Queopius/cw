from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cw.core.errors import CwError, ErrorCode
from cw.update.config import load_global_document, write_global_document


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    heartbeat_seconds: int = 60
    quiet_threshold_seconds: int = 90


def load_observability_settings() -> ObservabilitySettings:
    section = load_global_document().get("observability", {})
    if not isinstance(section, dict):
        raise CwError("[observability] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    unknown = set(section) - {"heartbeat_seconds", "quiet_threshold_seconds"}
    if unknown:
        raise CwError("Unknown observability setting", ErrorCode.USAGE_ERROR, details=", ".join(sorted(unknown)), exit_code=2)
    heartbeat = _bounded(section.get("heartbeat_seconds", 60), "heartbeat_seconds", minimum=10)
    quiet = _bounded(section.get("quiet_threshold_seconds", 90), "quiet_threshold_seconds", minimum=30)
    if quiet < heartbeat:
        raise CwError("Quiet threshold cannot be shorter than heartbeat interval", ErrorCode.USAGE_ERROR, exit_code=2)
    return ObservabilitySettings(heartbeat, quiet)


def set_observability_setting(key: str, raw: str) -> tuple[Any, ObservabilitySettings]:
    if key not in {"observability.heartbeat_seconds", "observability.quiet_threshold_seconds"}:
        raise CwError(f"Unknown observability setting: {key}", ErrorCode.USAGE_ERROR, exit_code=2)
    try:
        value = int(raw)
    except ValueError as exc:
        raise CwError(f"{key} must be an integer", ErrorCode.USAGE_ERROR, exit_code=2) from exc
    minimum = 10 if key.endswith("heartbeat_seconds") else 30
    _bounded(value, key, minimum=minimum)
    document = load_global_document()
    section = document.setdefault("observability", {})
    if not isinstance(section, dict):
        raise CwError("[observability] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    section[key.split(".", 1)[1]] = value
    candidate = ObservabilitySettings(
        int(section.get("heartbeat_seconds", 60)),
        int(section.get("quiet_threshold_seconds", 90)),
    )
    if candidate.quiet_threshold_seconds < candidate.heartbeat_seconds:
        raise CwError("Quiet threshold cannot be shorter than heartbeat interval", ErrorCode.USAGE_ERROR, exit_code=2)
    write_global_document(document)
    return value, load_observability_settings()


def _bounded(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > 3600:
        raise CwError(f"Observability setting {name} must be between {minimum} and 3600 seconds", ErrorCode.USAGE_ERROR, exit_code=2)
    return value
