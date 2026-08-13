from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised on Python 3.10
    tomllib = None  # type: ignore[assignment]


def load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    if tomllib is not None:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        return value if isinstance(value, dict) else {}
    return _load_minimal(path.read_text(encoding="utf-8"))


def _load_minimal(text: str) -> dict[str, Any]:
    """Parse CW's documented TOML subset on Python 3.10.

    The fallback intentionally accepts tables, booleans, integers, quoted
    strings, and literal arrays only. Unsupported TOML fails instead of being
    interpreted approximately.
    """
    root: dict[str, Any] = {}
    current = root
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]") and not line.startswith("[["):
            parts = [part.strip() for part in line[1:-1].split(".")]
            if not parts or any(not part for part in parts):
                raise ValueError(f"invalid table on line {line_number}")
            current = root
            for part in parts:
                existing = current.setdefault(part, {})
                if not isinstance(existing, dict):
                    raise ValueError(f"table conflicts with value on line {line_number}")
                current = existing
            continue
        if "=" not in line:
            raise ValueError(f"unsupported syntax on line {line_number}")
        key, rendered = (part.strip() for part in line.split("=", 1))
        if not key or key in current:
            raise ValueError(f"invalid or duplicate key on line {line_number}")
        current[key] = _value(rendered, line_number)
    return root


def _value(rendered: str, line_number: int) -> Any:
    if rendered in {"true", "false"}:
        return rendered == "true"
    if rendered.lstrip("-").isdigit():
        return int(rendered)
    if rendered.startswith("[") and rendered.endswith("]"):
        value = ast.literal_eval(rendered)
        if not isinstance(value, list):
            raise ValueError(f"invalid array on line {line_number}")
        return value
    if len(rendered) >= 2 and rendered[0] == rendered[-1] and rendered[0] in {'"', "'"}:
        value = ast.literal_eval(rendered)
        if not isinstance(value, str):
            raise ValueError(f"invalid string on line {line_number}")
        return value
    raise ValueError(f"unsupported value on line {line_number}")
