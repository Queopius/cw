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
