from __future__ import annotations

from typing import Any

from cw import __version__
from .errors import CwError, ErrorCode


SCHEMA_VERSION = 1


def schema_version(data: Any, kind: str, *, allow_legacy: bool = False) -> int:
    if not isinstance(data, dict):
        raise CwError(f"{kind} must contain an object", ErrorCode.SCHEMA_VALIDATION_ERROR)
    value = data.get("schema_version")
    if value is None and allow_legacy:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CwError(
            f"{kind} schema version is invalid",
            ErrorCode.SCHEMA_VERSION_ERROR,
            "Run: cw repair" if value is None else None,
            details=f"Expected: {SCHEMA_VERSION}\nFound: {value!r}",
        )
    if value > SCHEMA_VERSION:
        raise CwError(
            f"{kind} was created by a newer CW schema",
            ErrorCode.SCHEMA_VERSION_ERROR,
            "Upgrade CW before continuing.",
            details=f"Supported: {SCHEMA_VERSION}\nFound: {value}",
        )
    if value < SCHEMA_VERSION:
        raise CwError(
            f"{kind} schema requires migration",
            ErrorCode.SCHEMA_VERSION_ERROR,
            "Run: cw repair",
            details=f"Supported: {SCHEMA_VERSION}\nFound: {value}",
        )
    return value


def migrate_legacy_document(data: Any, kind: str) -> tuple[dict[str, Any], bool]:
    version = schema_version(data, kind, allow_legacy=True)
    assert isinstance(data, dict)
    if version == SCHEMA_VERSION:
        return data, False
    migrated = dict(data)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated.setdefault("cw_version", __version__)
    return migrated, True
