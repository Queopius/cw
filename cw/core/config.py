from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .models import Workflow

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
    if tomllib is not None:
        try:
            with path.open("rb") as stream:
                value = tomllib.load(stream)
        except Exception as exc:
            raise CwError(
                "Configuration file is invalid TOML",
                ErrorCode.USAGE_ERROR,
                details=f"{path}: {exc}",
                exit_code=2,
            ) from exc
        return value if isinstance(value, dict) else {}
    values: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line or line.startswith("["):
                raise ValueError(f"unsupported syntax on line {line_number}")
            key, rendered = (part.strip() for part in line.split("=", 1))
            if not key or key in values:
                raise ValueError(f"invalid or duplicate key on line {line_number}")
            if rendered in {"true", "false"}:
                values[key] = rendered == "true"
            elif rendered.lstrip("-").isdigit():
                values[key] = int(rendered)
            elif rendered.startswith("["):
                import ast
                values[key] = ast.literal_eval(rendered)
            elif len(rendered) >= 2 and rendered[0] == rendered[-1] and rendered[0] in {'"', "'"}:
                values[key] = rendered[1:-1]
            else:
                raise ValueError(f"unsupported value on line {line_number}")
    except (OSError, SyntaxError, ValueError) as exc:
        raise CwError(
            "Configuration file is invalid TOML",
            ErrorCode.USAGE_ERROR,
            details=f"{path}: {exc}",
            exit_code=2,
        ) from exc
    return values


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
    global_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cw" / "config.toml"
    project_path = root / ".cw" / "config.toml"
    for path in (global_path, project_path):
        source = _toml(path)
        _validate(source, path)
        config.update(source)
    return config


def load_policy(root: Path, *, workflow: Workflow | None = None) -> Policy:
    config = load_config(root, workflow=workflow)
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
    return Policy(
        max_review_attempts=config["max_review_attempts"],
        allow_network=config["allow_network"],
        protected_paths=tuple(config["protected_paths"]),
        human_gate_categories=tuple(config["human_gate_categories"]),
        command_timeout=config["command_timeout"],
        review_timeout=config["review_timeout"],
    )


def apply_policy(workflow: Workflow, policy: Policy) -> Workflow:
    return replace(
        workflow,
        max_review_attempts=policy.max_review_attempts,
        command_timeout=policy.command_timeout,
        review_timeout=policy.review_timeout,
    )
