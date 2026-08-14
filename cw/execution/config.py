from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cw.core.errors import CwError, ErrorCode
from cw.core.toml import load_toml
from cw.update.config import load_global_document, write_global_document
from cw.core.platform import global_config_dir

from .duration import parse_duration


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    default_phases: int = 1
    recommended_max_phases: int = 3
    hard_max_phases: int = 10
    default_max_time_seconds: int = 7200
    max_semantic_revisions_per_phase: int = 3


def load_execution_settings(root: Path) -> ExecutionSettings:
    global_path = global_config_dir() / "config.toml"
    project_path = root / ".cw/config.toml"
    global_values = _section(global_path)
    project_values = _section(project_path)
    defaults = ExecutionSettings()
    default_phases = _positive(global_values.get("default_phases", defaults.default_phases), "default_phases")
    recommended = _positive(global_values.get("recommended_max_phases", defaults.recommended_max_phases), "recommended_max_phases")
    hard = _positive(global_values.get("hard_max_phases", defaults.hard_max_phases), "hard_max_phases")
    revisions = _positive(global_values.get("max_semantic_revisions_per_phase", defaults.max_semantic_revisions_per_phase), "max_semantic_revisions_per_phase")
    max_time = parse_duration(str(global_values.get("default_max_time", "2h")))
    # Project policy may only reduce global safety ceilings.
    if "max_phases" in project_values:
        hard = min(hard, _positive(project_values["max_phases"], "max_phases"))
    if "max_time" in project_values:
        max_time = min(max_time, parse_duration(str(project_values["max_time"])))
    if "max_semantic_revisions_per_phase" in project_values:
        revisions = min(revisions, _positive(project_values["max_semantic_revisions_per_phase"], "max_semantic_revisions_per_phase"))
    if default_phases > hard or recommended > hard:
        default_phases, recommended = min(default_phases, hard), min(recommended, hard)
    return ExecutionSettings(default_phases, recommended, hard, max_time, revisions)


def set_execution_setting(key: str, raw: str) -> tuple[Any, ExecutionSettings]:
    allowed = {
        "execution.default_phases", "execution.recommended_max_phases",
        "execution.hard_max_phases", "execution.default_max_time",
        "execution.max_semantic_revisions_per_phase",
    }
    if key not in allowed:
        raise CwError(f"Unknown execution setting: {key}", ErrorCode.USAGE_ERROR, exit_code=2)
    leaf = key.split(".", 1)[1]
    if leaf == "default_max_time":
        parse_duration(raw)
        value: Any = raw.lower()
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise CwError(f"{key} must be a positive integer", ErrorCode.USAGE_ERROR, exit_code=2) from exc
        _positive(value, leaf)
    document = load_global_document()
    section = document.setdefault("execution", {})
    if not isinstance(section, dict):
        raise CwError("[execution] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    section[leaf] = value
    # Validate cross-field safety before replacing the user's global file.
    defaults = ExecutionSettings()
    default_phases = int(section.get("default_phases", defaults.default_phases))
    recommended = int(section.get("recommended_max_phases", defaults.recommended_max_phases))
    hard = int(section.get("hard_max_phases", defaults.hard_max_phases))
    if default_phases > hard or recommended > hard:
        raise CwError("Default and recommended phase counts cannot exceed the hard cap", ErrorCode.USAGE_ERROR, exit_code=2)
    write_global_document(document)
    return value, load_execution_settings(Path.cwd())


def _section(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = load_toml(path)
    except Exception as exc:
        raise CwError("Execution configuration is invalid", ErrorCode.USAGE_ERROR, details=str(exc), exit_code=2) from exc
    section = document.get("execution", {})
    if not isinstance(section, dict):
        raise CwError("[execution] must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    allowed = {
        "default_phases", "recommended_max_phases", "hard_max_phases",
        "default_max_time", "max_semantic_revisions_per_phase",
        "max_phases", "max_time", "require_clean_git",
    }
    if set(section) - allowed:
        raise CwError("Unknown execution setting", ErrorCode.USAGE_ERROR, details=", ".join(sorted(set(section) - allowed)), exit_code=2)
    return section


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CwError(f"Execution setting {name} must be a positive integer", ErrorCode.USAGE_ERROR, exit_code=2)
    return value
