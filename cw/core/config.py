from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .layout import safe_file
from .models import Workflow
from .utils import atomic_write, safe_project_path
from .toml import load_toml
from .platform import global_config_dir

CORE_PROTECTED_PATHS = (
    ".cw/state.json",
    ".cw/project.json",
    ".cw/config.toml",
    ".cw/gates",
    ".cw/reviews",
    ".cw/completion",
    ".cw/plan-revisions",
    ".cw/plan-proposals",
    ".cw/supersessions",
    ".cw/evidence-supersessions",
    ".cw/plan-amendments",
    ".cw/runtime/plan-rebaseline-transaction.json",
    ".cw/runtime/plan-amend-transaction.json",
    ".codex/workflow/phases.yaml",
)


DEFAULTS: dict[str, Any] = {
    "max_review_attempts": 3,
    "allow_network": False,
    "protected_paths": list(CORE_PROTECTED_PATHS),
    "human_gate_categories": [
        "payments", "cryptography", "destructive-migration", "production",
        "authentication-security", "public-api-breaking", "infrastructure-deletion",
    ],
    "command_timeout": 1200,
    "review_timeout": 1200,
}


@dataclass(frozen=True, slots=True)
class Policy:
    max_review_attempts: int
    allow_network: bool
    protected_paths: tuple[str, ...]
    human_gate_categories: tuple[str, ...]
    command_timeout: int
    review_timeout: int


def _toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return load_toml(path)
    except (OSError, SyntaxError, ValueError) as exc:
        raise CwError(
            "Configuration file is invalid TOML",
            ErrorCode.USAGE_ERROR,
            details=f"{path}: {exc}",
            exit_code=2,
        ) from exc


def _validate(source: dict[str, Any], path: Path) -> None:
    unknown = set(source) - set(DEFAULTS)
    if unknown:
        raise CwError(
            f"Unknown configuration setting: {', '.join(sorted(unknown))}",
            ErrorCode.USAGE_ERROR,
            details=str(path),
            exit_code=2,
        )


def load_config(root: Path, *, workflow: Workflow | None = None) -> dict[str, Any]:
    config = dict(DEFAULTS)
    if workflow is not None:
        config.update({
            "max_review_attempts": workflow.max_review_attempts,
            "command_timeout": workflow.command_timeout,
            "review_timeout": workflow.review_timeout,
        })
    global_path = global_config_dir() / "config.toml"
    project_path = root / ".cw" / "config.toml"
    safe_file(project_path, ".cw/config.toml")
    for path in (global_path, project_path):
        source = _toml(path)
        if path == global_path:
            source = {key: value for key, value in source.items() if key not in {"updates", "execution"}}
        else:
            source = {key: value for key, value in source.items() if key not in {"integrations", "execution"}}
        _validate(source, path)
        config.update(source)
    return config


def _policy_from_config(root: Path, config: dict[str, Any]) -> Policy:
    for key in ("max_review_attempts", "command_timeout", "review_timeout"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CwError(
                f"Configuration setting {key} must be a positive integer",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
    if not isinstance(config["allow_network"], bool):
        raise CwError("Configuration setting allow_network must be a boolean", ErrorCode.USAGE_ERROR, exit_code=2)
    for key in ("protected_paths", "human_gate_categories"):
        value = config[key]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise CwError(f"Configuration setting {key} must be a string list", ErrorCode.USAGE_ERROR, exit_code=2)
    for value in config["protected_paths"]:
        if any(character in value for character in "*?["):
            raise CwError("Configuration setting protected_paths cannot contain globs", ErrorCode.USAGE_ERROR, exit_code=2)
        try:
            path = safe_project_path(root, value)
        except CwError as exc:
            raise CwError(
                "Configuration setting protected_paths must be repository-relative",
                ErrorCode.USAGE_ERROR,
                details=exc.message,
                exit_code=2,
            ) from exc
        if path.is_symlink():
            raise CwError("Configuration setting protected_paths cannot contain symlinks", ErrorCode.USAGE_ERROR, exit_code=2)
    return Policy(
        max_review_attempts=config["max_review_attempts"],
        allow_network=config["allow_network"],
        protected_paths=tuple(dict.fromkeys((*CORE_PROTECTED_PATHS, *config["protected_paths"]))),
        human_gate_categories=tuple(config["human_gate_categories"]),
        command_timeout=config["command_timeout"],
        review_timeout=config["review_timeout"],
    )


def load_policy(root: Path, *, workflow: Workflow | None = None) -> Policy:
    return _policy_from_config(root, load_config(root, workflow=workflow))


def _parse_setting(key: str, raw_value: str) -> Any:
    if key not in DEFAULTS:
        raise CwError(
            f"Unknown configuration setting: {key}",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    expected = DEFAULTS[key]
    if isinstance(expected, bool):
        normalized = raw_value.lower()
        if normalized not in {"true", "false"}:
            raise CwError(
                f"Configuration setting {key} must be true or false",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
        return normalized == "true"
    if isinstance(expected, int):
        try:
            return int(raw_value)
        except ValueError as exc:
            raise CwError(
                f"Configuration setting {key} must be a positive integer",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            ) from exc
    if isinstance(expected, list):
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise CwError(
                f"Configuration setting {key} must be a JSON string list",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            ) from exc
        return value
    raise CwError(f"Configuration setting {key} cannot be changed", ErrorCode.USAGE_ERROR, exit_code=2)


def _render_toml(config: dict[str, Any]) -> str:
    lines = ["# CW project overrides"]
    for key in DEFAULTS:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key} = {rendered}")
    integrations = config.get("integrations")
    if isinstance(integrations, dict):
        for name, settings in integrations.items():
            if not isinstance(settings, dict):
                continue
            lines.extend(["", f"[integrations.{name}]"])
            if "required" in settings:
                lines.append(f"required = {'true' if settings['required'] else 'false'}")
    execution = config.get("execution")
    if isinstance(execution, dict):
        lines.extend(["", "[execution]"])
        for key in ("max_phases", "max_time", "max_semantic_revisions_per_phase", "require_clean_git"):
            if key in execution:
                value = execution[key]
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif isinstance(value, int):
                    rendered = str(value)
                else:
                    rendered = json.dumps(value)
                lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def set_project_config(root: Path, workflow: Workflow, key: str, raw_value: str) -> tuple[Any, dict[str, Any]]:
    global_path = global_config_dir() / "config.toml"
    project_path = root / ".cw" / "config.toml"
    safe_file(project_path, ".cw/config.toml")
    project = _toml(project_path)
    _validate({key: value for key, value in project.items() if key not in {"integrations", "execution"}}, project_path)
    project[key] = _parse_setting(key, raw_value)

    effective = dict(DEFAULTS)
    effective.update({
        "max_review_attempts": workflow.max_review_attempts,
        "command_timeout": workflow.command_timeout,
        "review_timeout": workflow.review_timeout,
    })
    global_config = {key: value for key, value in _toml(global_path).items() if key not in {"updates", "execution"}}
    _validate(global_config, global_path)
    effective.update(global_config)
    effective.update(project)
    _policy_from_config(root, effective)

    atomic_write(project_path, _render_toml(project))
    return project[key], effective


def apply_policy(workflow: Workflow, policy: Policy) -> Workflow:
    return replace(
        workflow,
        max_review_attempts=policy.max_review_attempts,
        command_timeout=policy.command_timeout,
        review_timeout=policy.review_timeout,
        allow_network=policy.allow_network,
        protected_paths=policy.protected_paths,
        human_gate_categories=policy.human_gate_categories,
    )
