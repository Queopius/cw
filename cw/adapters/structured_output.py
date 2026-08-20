from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cw.core.errors import CwError, ErrorCode


# Codex structured output accepts a strict JSON Schema subset. CW retains its
# richer internal contracts and enforces semantic constraints in the domain.
UNSUPPORTED_CODEX_SCHEMA_KEYWORDS = frozenset({"uniqueItems"})


def codex_schema(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / "codex" / name


def _unsupported(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            location = f"{path}.{key}"
            if key in UNSUPPORTED_CODEX_SCHEMA_KEYWORDS:
                found.append(location)
            found.extend(_unsupported(child, location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_unsupported(child, f"{path}[{index}]"))
    return found


def validate_codex_output_schema(path: Path, *, role: str) -> dict[str, Any]:
    """Fail locally if an internal-only constraint leaks into a Codex schema."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        code = ErrorCode.PLANNER_SCHEMA_ERROR if role.endswith("planner") else ErrorCode.REVIEWER_PROCESS_ERROR
        raise CwError(
            f"{role.title()} output schema is invalid",
            code,
            "Run: cw error",
            details=str(exc),
        ) from exc
    if not isinstance(payload, dict):
        paths = ["$"]
    else:
        paths = _unsupported(payload)
    if paths:
        code = ErrorCode.PLANNER_SCHEMA_ERROR if role.endswith("planner") else ErrorCode.REVIEWER_PROCESS_ERROR
        raise CwError(
            f"{role.title()} output schema is incompatible with Codex",
            code,
            "Run: cw error",
            details="Unsupported structured-output keywords: " + ", ".join(paths),
        )
    return payload
