from __future__ import annotations

from pathlib import Path

from cw.core.errors import CwError, ErrorCode
from cw.core.toml import load_toml


def project_requirements(root: Path) -> set[str]:
    path = root / ".cw/config.toml"
    if not path.is_file():
        return set()
    try:
        document = load_toml(path)
    except Exception as exc:
        raise CwError("Project integration configuration is invalid", ErrorCode.USAGE_ERROR, details=str(exc), exit_code=2) from exc
    integrations = document.get("integrations", {})
    if integrations is None:
        return set()
    if not isinstance(integrations, dict):
        raise CwError("[integrations] must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    required: set[str] = set()
    for name, value in integrations.items():
        if not isinstance(value, dict) or set(value) - {"required"} or not isinstance(value.get("required", False), bool):
            raise CwError(f"Integration configuration is invalid: {name}", ErrorCode.USAGE_ERROR, exit_code=2)
        if value.get("required"):
            required.add(str(name))
    return required
