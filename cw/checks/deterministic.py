from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from cw.core.commands import command_arguments
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import artifact_hashes, validate_dependencies
from cw.core.models import Phase, ValidationResult, Workflow
from cw.core.session import load_session, readiness_path
from cw.core.schema import schema_version
from cw.core.utils import load_json, safe_project_path
from cw.execution.context import current_event_sink
from cw.execution.events import ExecutionEvent, ExecutionEventType


def load_readiness(root: Path, phase: Phase) -> dict[str, Any]:
    path = readiness_path(root)
    if not path.is_file() or path.is_symlink():
        raise CwError("Readiness manifest is missing", ErrorCode.SCHEMA_VALIDATION_ERROR, "Complete the phase and create .cw/runtime/READY_FOR_REVIEW.json")
    data = load_json(path)
    if not isinstance(data, dict):
        raise CwError("Readiness manifest must be an object", ErrorCode.SCHEMA_VALIDATION_ERROR)
    schema_version(data, "Readiness manifest")
    allowed = {"schema_version", "session_id", "phase", "status", "artifacts", "checks_executed"}
    if set(data) - allowed:
        raise CwError("Readiness manifest has unknown fields", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if not isinstance(data.get("session_id"), str) or re.fullmatch(r"[0-9a-f]{32}", data["session_id"]) is None:
        raise CwError("Readiness session ID is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if data.get("phase") != phase.id or data.get("status") != "READY_FOR_REVIEW":
        raise CwError("Readiness manifest phase or status mismatch", ErrorCode.SCHEMA_VALIDATION_ERROR)
    artifacts = data.get("artifacts")
    checks = data.get("checks_executed")
    if not isinstance(artifacts, list) or not all(isinstance(v, str) for v in artifacts):
        raise CwError("Readiness artifacts must be a string list", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if set(artifacts) - set(phase.artifacts):
        raise CwError("Readiness contains unknown artifacts", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if not isinstance(checks, list):
        raise CwError("checks_executed must be a list", ErrorCode.SCHEMA_VALIDATION_ERROR)
    approved_commands = {item.command for item in phase.required_commands}
    for item in checks:
        if not isinstance(item, dict) or set(item) - {"command", "exit_code"}:
            raise CwError("Invalid readiness check entry", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if item.get("command") not in approved_commands:
            raise CwError("Readiness contains an arbitrary command", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if not isinstance(item.get("exit_code"), int):
            raise CwError("Readiness check exit code is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    for artifact in artifacts:
        safe_project_path(root, artifact, must_exist=True)
    return data


def _redacted_environment() -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "LANG", "LC_ALL", "TERM", "TMPDIR", "TMP", "TEMP", "CI", "HOME",
        "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "LOCALAPPDATA", "APPDATA",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _validate_completed_work(root: Path, workflow: Workflow, phase: Phase, result: ValidationResult) -> None:
    validate_dependencies(root, workflow, phase)
    result.checks.append({"name": "Previous gates", "status": "passed"})
    for artifact in phase.artifacts:
        safe_project_path(root, artifact, must_exist=True)
    result.checks.append({"name": "Artifacts", "status": "passed", "count": len(phase.artifacts)})
    for command in phase.required_commands:
        timeout = command.timeout_seconds or workflow.command_timeout
        arguments = command_arguments(command.command)
        sink = current_event_sink()
        if sink is not None:
            sink(ExecutionEvent(
                ExecutionEventType.COMMAND_STARTED,
                source_type="cw.validation.command",
                command=command.command,
                status="in_progress",
            ))
        command_started = time.monotonic()
        completed = subprocess.run(
            arguments, cwd=root, shell=False, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=timeout, env=_redacted_environment(), check=False,
            stdin=subprocess.DEVNULL,
        )
        if sink is not None:
            sink(ExecutionEvent(
                ExecutionEventType.COMMAND_COMPLETED,
                source_type="cw.validation.command",
                command=command.command,
                exit_code=completed.returncode,
                duration_ms=max(0, round((time.monotonic() - command_started) * 1000)),
                status="completed" if completed.returncode == 0 else "failed",
            ))
        check = {"name": "Required command", "command": command.command, "exit_code": completed.returncode}
        result.checks.append(check)
        if completed.returncode:
            raise CwError(f"Required command failed: {command.command}", details=(completed.stderr or completed.stdout)[-4000:])
    validate_dependencies(root, workflow, phase)
    result.artifact_hashes = artifact_hashes(root, phase.artifacts)
    result.checks.append({"name": "SHA-256 integrity", "status": "passed"})


def inspect_completed_work(root: Path, workflow: Workflow, phase: Phase) -> ValidationResult:
    """Validate implemented work without trusting or requiring a readiness manifest."""
    result = ValidationResult(passed=False)
    try:
        _validate_completed_work(root, workflow, phase, result)
        result.passed = True
    except (CwError, OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            message = f"Required command timed out: {exc.cmd}"
        elif isinstance(exc, OSError):
            message = f"Required command could not start: {exc}"
        else:
            message = str(exc)
        result.errors.append(message)
        result.checks.append({"name": "Validation", "status": "failed", "detail": message})
    return result


def validate_phase(root: Path, workflow: Workflow, phase: Phase) -> ValidationResult:
    result = ValidationResult(passed=False)
    try:
        manifest = load_readiness(root, phase)
        session = load_session(root, workflow, phase)
        if session is None:
            raise CwError("Readiness manifest has no active implementer session", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if manifest["session_id"] != session["session_id"]:
            raise CwError("Readiness manifest does not belong to the active implementer session", ErrorCode.SCHEMA_VALIDATION_ERROR)
        result.checks.append({"name": "Manifest", "status": "passed"})
        required = set(phase.artifacts)
        declared = set(manifest["artifacts"])
        missing = required - declared
        if missing:
            raise CwError(f"Required artifacts are not declared: {', '.join(sorted(missing))}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        # Establish existence before commands, recheck dependency gates after
        # commands, and bind final file bytes into the semantic review.
        _validate_completed_work(root, workflow, phase, result)
        result.passed = True
    except (CwError, OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            message = f"Required command timed out: {exc.cmd}"
        elif isinstance(exc, OSError):
            message = f"Required command could not start: {exc}"
        else:
            message = str(exc)
        result.errors.append(message)
        result.checks.append({"name": "Validation", "status": "failed", "detail": message})
    return result
