from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from cw.ui.console import Console, emit_json
from cw.core.errors import CwError, ErrorCode
from cw.core.platform import process_is_alive
from cw.ui.renderers import (
    render_changelog, render_rollback, render_update_check, render_update_info,
    render_update_result,
)
from cw.update.service import UpdateService
from cw.execution.session import load_batch


ServiceFactory = Callable[[], UpdateService]


def _payload(info: Any) -> dict[str, Any]:
    return {
        "installed": str(info.installed),
        "latest": str(info.latest),
        "available": info.available,
        "level": info.level,
        "channel": info.channel,
        "published_at": info.manifest.published_at,
        "summary": info.manifest.summary,
        "release_url": info.manifest.release_url,
        "minimum_project_schema": info.manifest.minimum_project_schema,
        "maximum_project_schema": info.manifest.maximum_project_schema,
        "signature_present": info.manifest.signature is not None,
    }


def command_update(
    args: argparse.Namespace,
    console: Console,
    *,
    service_factory: ServiceFactory = UpdateService.default,
) -> int:
    service = service_factory()
    batch = load_batch(Path.cwd()) if (Path.cwd() / ".cw").is_dir() else None
    if batch and batch.get("status") == "RUNNING" and isinstance(batch.get("pid"), int) and _alive(batch["pid"]):
        raise CwError(
            "CW cannot update during an active batch",
            ErrorCode.UPDATE_INCOMPATIBLE,
            "Wait for the batch to stop safely",
            exit_code=3,
        )
    if args.channel:
        service.settings = replace(service.settings, channel=args.channel)
    if args.rollback:
        result = service.rollback()
        payload = {
            "action": "rollback", "previous": result.previous,
            "current": result.current, "rollback_available": result.rollback_available,
        }
        if args.json:
            emit_json(payload)
        else:
            render_rollback(console, payload)
        return 0
    if args.check or args.info:
        info = service.info(force=args.check)
        payload = _payload(info)
        if args.json:
            emit_json(payload)
        elif args.info:
            render_update_info(console, payload)
        else:
            render_update_check(console, payload)
        return 0
    info, result = service.install(
        requested_version=args.version,
        with_remote=bool(getattr(args, "with_remote", False)),
    )
    payload = _payload(info)
    payload.update({
        "installed_now": result is not None,
        "previous": result.previous if result else None,
        "current": result.current if result else str(info.installed),
        "rollback_available": result.rollback_available if result else False,
    })
    if args.json:
        emit_json(payload)
    else:
        render_update_result(console, payload)
    return 0


def command_changelog(args: argparse.Namespace, console: Console) -> int:
    document = json.loads(files("cw").joinpath("release_history.json").read_text(encoding="utf-8"))
    releases = document.get("releases", [])
    if args.json:
        emit_json({"schema_version": 1, "releases": releases})
    else:
        render_changelog(console, releases)
    return 0


def _alive(pid: int) -> bool:
    return process_is_alive(pid)
