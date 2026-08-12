from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python 3.10 support without a runtime dependency.
    tomllib = None  # type: ignore[assignment]


DEFAULTS: dict[str, Any] = {
    "max_review_attempts": 3,
    "allow_network": False,
    "protected_paths": [".cw/gates", ".cw/reviews", ".codex/workflow/phases.yaml"],
    "human_gate_categories": ["payments", "cryptography", "destructive-migration", "production"],
    "command_timeout": 1200,
    "review_timeout": 1200,
}


def _toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    if tomllib is not None:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        return value if isinstance(value, dict) else {}
    values: dict[str, Any] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line or line.startswith("["):
            continue
        key, rendered = (part.strip() for part in line.split("=", 1))
        if rendered in {"true", "false"}:
            values[key] = rendered == "true"
        elif rendered.isdigit():
            values[key] = int(rendered)
        elif rendered.startswith("["):
            import json
            values[key] = json.loads(rendered)
        else:
            values[key] = rendered.strip('"\'')
    return values


def load_config(root: Path) -> dict[str, Any]:
    config = dict(DEFAULTS)
    global_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cw" / "config.toml"
    config.update(_toml(global_path))
    config.update(_toml(root / ".cw" / "config.toml"))
    return config
