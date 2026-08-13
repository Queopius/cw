from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from cw.core.config import load_config, set_project_config
from cw.core.errors import CwError, ErrorCode
from cw.core.locking import operation_lock
from cw.core.project import load_project
from cw.core.workflow import load_workflow
from cw.ui.console import Console, emit_json
from cw.execution.config import load_execution_settings, set_execution_setting
from cw.execution.observability import load_observability_settings, set_observability_setting
from cw.update.config import load_update_settings, set_update_setting


RootResolver = Callable[[], Path]


def _validate_identity(root: Path) -> Any:
    project = load_project(root)
    workflow = load_workflow(root)
    if workflow.id != project.project_id or workflow.repository != project.project_id:
        raise CwError(
            "Project workflow mismatch",
            ErrorCode.WORKFLOW_PROJECT_MISMATCH,
            "Run: cw repair",
            details=f"Workflow: {workflow.repository or workflow.id}\nRepository: {project.project_id}",
        )
    return workflow


def _render_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    return value


def command_config(args: argparse.Namespace, console: Console, *, root_resolver: RootResolver) -> int:
    if args.action == "set" and isinstance(args.key, str) and args.key.startswith("observability."):
        if args.value is None:
            raise CwError("Configuration value is required", ErrorCode.USAGE_ERROR, exit_code=2)
        value, settings = set_observability_setting(args.key, args.value)
        payload = {
            "scope": "global", "setting": args.key, "value": value,
            "observability": {
                "heartbeat_seconds": settings.heartbeat_seconds,
                "quiet_threshold_seconds": settings.quiet_threshold_seconds,
            },
            "path": "~/.config/cw/config.toml",
        }
        if args.json:
            emit_json(payload)
        else:
            console.header("Configuration")
            console.item("✓", "Global observability setting updated")
            console.field("Setting", args.key)
            console.field("Value", value)
            console.field("File", "~/.config/cw/config.toml")
        return 0
    if args.action == "set" and isinstance(args.key, str) and args.key.startswith("execution."):
        if args.value is None:
            raise CwError("Configuration value is required", ErrorCode.USAGE_ERROR, exit_code=2)
        value, settings = set_execution_setting(args.key, args.value)
        payload = {
            "scope": "global", "setting": args.key, "value": value,
            "execution": {
                "default_phases": settings.default_phases,
                "recommended_max_phases": settings.recommended_max_phases,
                "hard_max_phases": settings.hard_max_phases,
                "default_max_time_seconds": settings.default_max_time_seconds,
                "max_semantic_revisions_per_phase": settings.max_semantic_revisions_per_phase,
            },
            "path": "~/.config/cw/config.toml",
        }
        if args.json:
            emit_json(payload)
        else:
            console.header("Configuration")
            console.item("✓", "Global execution setting updated")
            console.field("Setting", args.key)
            console.field("Value", _render_value(value))
            console.field("File", "~/.config/cw/config.toml")
        return 0
    if args.action == "set" and isinstance(args.key, str) and args.key.startswith("updates."):
        if args.value is None:
            raise CwError("Configuration value is required", ErrorCode.USAGE_ERROR, exit_code=2)
        value, settings = set_update_setting(args.key, args.value)
        payload = {
            "scope": "global", "setting": args.key, "value": value,
            "updates": {
                "channel": settings.channel, "check": settings.check,
                "check_interval_hours": settings.check_interval_hours,
            },
            "path": "~/.config/cw/config.toml",
        }
        if args.json:
            emit_json(payload)
        else:
            console.header("Configuration")
            console.item("✓", "Global update setting updated")
            console.field("Setting", args.key)
            console.field("Value", _render_value(value))
            console.field("File", "~/.config/cw/config.toml")
        return 0
    root = root_resolver()
    workflow = _validate_identity(root)
    if args.action == "set":
        if args.key is None or args.value is None:
            raise CwError(
                "Configuration setting and value are required",
                ErrorCode.USAGE_ERROR,
                "Run: cw config set <setting> <value>",
                exit_code=2,
            )
        with operation_lock(root, "config-set"):
            value, config = set_project_config(root, workflow, args.key, args.value)
        payload = {
            "scope": "project",
            "setting": args.key,
            "value": value,
            "effective": config,
            "path": ".cw/config.toml",
        }
        if args.json:
            emit_json(payload)
        else:
            console.header("Configuration")
            console.item("✓", "Project setting updated")
            console.field("Setting", args.key)
            console.field("Value", _render_value(value))
            console.field("File", ".cw/config.toml")
        return 0
    config = load_config(root, workflow=workflow)
    update_settings = load_update_settings()
    config["updates"] = {
        "channel": update_settings.channel,
        "check": update_settings.check,
        "check_interval_hours": update_settings.check_interval_hours,
    }
    execution = load_execution_settings(root)
    config["execution"] = {
        "default_phases": execution.default_phases,
        "recommended_max_phases": execution.recommended_max_phases,
        "hard_max_phases": execution.hard_max_phases,
        "default_max_time_seconds": execution.default_max_time_seconds,
        "max_semantic_revisions_per_phase": execution.max_semantic_revisions_per_phase,
    }
    observability = load_observability_settings()
    config["observability"] = {
        "heartbeat_seconds": observability.heartbeat_seconds,
        "quiet_threshold_seconds": observability.quiet_threshold_seconds,
    }
    if args.json:
        emit_json(config)
    else:
        console.header("Configuration")
        for key, value in config.items():
            console.field(key, _render_value(value), 24)
        console.line()
        console.wrapped(
            "Precedence: defaults < global (~/.config/cw/config.toml) < "
            "project (.cw/config.toml) < command-line flags"
        )
    return 0
