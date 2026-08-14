from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .platform import fsync_directory


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CwError(f"Invalid JSON: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR, details=str(exc)) from exc


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_new(path: Path, content: str) -> None:
    """Atomically create *path* without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def atomic_json_new(path: Path, payload: Any) -> None:
    atomic_write_new(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def safe_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or not value or "\x00" in value:
        raise CwError(f"Unsafe project path: {value}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    path = root / candidate
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise CwError(f"Path escapes repository: {value}", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc
    if must_exist:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise CwError(f"Artifact missing or outside repository: {value}", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc
    return path
