from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .schema import SCHEMA_VERSION
from .utils import atomic_json, load_json, utc_now


LAST_ERROR = ".cw/logs/last-error.json"
ERROR_HISTORY = ".cw/logs/errors.jsonl"
_REDACTIONS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"(?i)(https?://[^\s/:@]+:)[^\s/@]+(@)"),
)


def redact(value: str | None) -> str | None:
    if value is None:
        return None
    result = value
    for index, pattern in enumerate(_REDACTIONS):
        if index == 0:
            result = pattern.sub(r"\1[REDACTED]", result)
        elif index == 1:
            result = pattern.sub(r"\1[REDACTED]", result)
        elif index == 2:
            result = pattern.sub("[REDACTED]", result)
        else:
            result = pattern.sub(r"\1[REDACTED]\2", result)
    return result


def _logs_directory(root: Path, *, create: bool) -> Path | None:
    runtime = root / ".cw"
    if not runtime.is_dir() or runtime.is_symlink():
        return None
    logs = runtime / "logs"
    if logs.exists() and (not logs.is_dir() or logs.is_symlink()):
        return None
    if create:
        logs.mkdir(mode=0o700, parents=False, exist_ok=True)
    return logs if logs.is_dir() else None


def _valid_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != SCHEMA_VERSION:
        return None
    if not all(isinstance(value.get(key), str) and value[key] for key in ("timestamp", "code", "message")):
        return None
    return value


def load_diagnostic(root: Path) -> dict[str, Any] | None:
    logs = _logs_directory(root, create=False)
    if logs is None:
        return None
    path = logs / "last-error.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return _valid_record(load_json(path))
    except CwError:
        return None


def _append_history(path: Path, record: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_diagnostic(
    root: Path,
    error: CwError,
    *,
    source: str | None = None,
    traceback_text: str | None = None,
) -> dict[str, Any] | None:
    logs = _logs_directory(root, create=True)
    if logs is None:
        return None
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_now(),
        "code": error.code.value,
        "message": redact(error.message),
        "hint": redact(error.hint),
        "details": redact(error.details),
        "source": redact(source),
        "traceback": redact(traceback_text),
    }
    previous = load_diagnostic(root)
    duplicate = previous is not None and all(
        previous.get(key) == record.get(key)
        for key in ("code", "message", "hint", "details", "source", "traceback")
    )
    atomic_json(logs / "last-error.json", record)
    if not duplicate:
        _append_history(logs / "errors.jsonl", record)
    return record


def global_diagnostic_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cw" / "last-error.json"


def record_global_diagnostic(
    error: CwError, *, source: str | None = None, traceback_text: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_now(),
        "code": error.code.value,
        "message": redact(error.message),
        "hint": redact(error.hint),
        "details": redact(error.details),
        "source": redact(source),
        "traceback": redact(traceback_text),
    }
    atomic_json(global_diagnostic_path(), record)
    return record


def load_global_diagnostic() -> dict[str, Any] | None:
    path = global_diagnostic_path()
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return _valid_record(load_json(path))
    except CwError:
        return None


def legacy_diagnostic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    first, _, remainder = value.partition(":")
    code = first.strip() if first.strip() in {item.value for item in ErrorCode} else ErrorCode.WORKFLOW_ERROR.value
    message, _, details = remainder.strip().partition("\n")
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": "unknown",
        "code": code,
        "message": redact(message or value.strip()),
        "hint": None,
        "details": redact(details) if details else None,
        "source": "legacy-state",
        "traceback": None,
    }


def raw_diagnostic(record: dict[str, Any]) -> str:
    lines = [f"{record['code']}: {record['message']}"]
    if record.get("details"):
        lines.append(str(record["details"]))
    if record.get("traceback"):
        lines.append(str(record["traceback"]))
    return "\n".join(lines)


def state_error(error: CwError) -> str:
    value = f"{error.code.value}: {redact(error.message)}"
    details = redact(error.details)
    return f"{value}\n{details}" if details else value
