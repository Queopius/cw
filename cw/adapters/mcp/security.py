from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cw.core.diagnostics import redact


_PRIVATE_KEY = re.compile(
    r"(?i)(?:authorization|credential|environment|password|raw[_-]?log|secret|token)"
)
_HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s\"']+")
_WINDOWS_HOME = re.compile(
    r"(?i)(?:\b[A-Z]:)?[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\r\n]+"
)


def sanitize(value: Any, *, private_roots: tuple[Path, ...]) -> Any:
    """Return an MCP-safe projection without secrets or private absolute roots."""

    roots = tuple(
        item for root in private_roots
        for item in (str(root), str(root).replace("/", "\\"))
        if item
    )

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            output: dict[str, Any] = {}
            for key, nested in item.items():
                label = str(key)
                if label in {"repository_root", "local_path"}:
                    continue
                if _PRIVATE_KEY.search(label):
                    output[label] = "[REDACTED]" if nested not in (None, "", [], {}) else nested
                else:
                    output[label] = clean(nested)
            return output
        if isinstance(item, (list, tuple)):
            return [clean(nested) for nested in item]
        if isinstance(item, str):
            result = redact(item) or ""
            for root in sorted(set(roots), key=len, reverse=True):
                result = re.sub(re.escape(root), "<PROJECT_ROOT>", result, flags=re.IGNORECASE)
            result = _WINDOWS_HOME.sub("~", result)
            return _HOME_PATH.sub("~", result)
        return item

    return clean(value)
