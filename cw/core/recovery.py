from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .diagnostics import record_diagnostic, redact
from .errors import CwError, ErrorCode
from .models import Phase, ValidationResult, Workflow
from .schema import SCHEMA_VERSION
from .session import create_session, load_session, readiness_path
from .utils import atomic_json, load_json, utc_now


RETRYABLE_OPERATIONS: dict[ErrorCode, str] = {
    ErrorCode.REVIEWER_NETWORK_ERROR: "review",
    ErrorCode.REVIEWER_PROCESS_ERROR: "review",
    ErrorCode.REVIEW_TIMEOUT: "review",
    ErrorCode.IMPLEMENTER_PROCESS_ERROR: "implementation",
    ErrorCode.PLANNER_NETWORK_ERROR: "planning",
    ErrorCode.PLANNER_PROCESS_ERROR: "planning",
    ErrorCode.PLAN_TIMEOUT: "planning",
    ErrorCode.CODEX_NOT_FOUND: "codex",
}


def infrastructure_error_metadata(
    error: CwError,
    *,
    operation: str,
    phase: str | None,
    occurred_at: str | None = None,
    legacy: bool = False,
) -> dict[str, Any]:
    return {
        "error_code": error.code.value,
        "retryable": True,
        "operation": operation,
        "phase": phase,
        "occurred_at": occurred_at or utc_now(),
        "legacy": legacy,
    }


def mark_infrastructure_error(
    state: dict[str, Any],
    error: CwError,
    *,
    operation: str,
    phase: str | None,
) -> dict[str, Any]:
    metadata = infrastructure_error_metadata(error, operation=operation, phase=phase)
    state["infrastructure_error"] = metadata
    return metadata


def _explicit_code(text: str) -> ErrorCode | None:
    prefix = text.strip().split(":", 1)[0]
    try:
        return ErrorCode(prefix)
    except ValueError:
        return None


def classify_legacy_infrastructure_error(
    value: Any,
    *,
    phase: str | None,
    operation: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any] | None:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, str):
        text = value
    else:
        return None
    if not text.strip():
        return None
    lowered = text.lower()
    code = _explicit_code(text)
    inferred_operation = operation
    if code in RETRYABLE_OPERATIONS:
        inferred_operation = inferred_operation or RETRYABLE_OPERATIONS[code]
    elif code is ErrorCode.SCHEMA_VALIDATION_ERROR and (
        inferred_operation == "review" or "reviewer" in lowered or "response schema" in lowered
    ):
        inferred_operation = "review"
    elif "timeout" in lowered or "timed out" in lowered:
        code = ErrorCode.REVIEW_TIMEOUT
        inferred_operation = inferred_operation or "review"
    elif any(signature in lowered for signature in (
        "websocket", "transport error", "https transport", "network unavailable", "connection reset",
    )):
        code = ErrorCode.REVIEWER_NETWORK_ERROR
        inferred_operation = inferred_operation or "review"
    elif any(signature in lowered for signature in (
        "invalid response schema", "reviewer result schema", "response schema is invalid",
    )):
        code = ErrorCode.SCHEMA_VALIDATION_ERROR
        inferred_operation = inferred_operation or "review"
    elif any(signature in lowered for signature in (
        "reviewer smoke test failed", "operation not permitted", "reviewer process crash",
        "reviewer process failed", "process crash",
    )):
        code = ErrorCode.REVIEWER_PROCESS_ERROR
        inferred_operation = inferred_operation or "review"
    else:
        return None
    if inferred_operation is None:
        return None
    error = CwError(text.strip().splitlines()[0], code or ErrorCode.WORKFLOW_ERROR)
    return infrastructure_error_metadata(
        error,
        operation=inferred_operation,
        phase=phase,
        occurred_at=occurred_at,
        legacy=True,
    )


def retryable_infrastructure_error(state: dict[str, Any]) -> dict[str, Any] | None:
    metadata = state.get("infrastructure_error")
    if isinstance(metadata, dict):
        required = {"error_code", "retryable", "operation", "phase", "occurred_at"}
        if (
            required.issubset(metadata)
            and metadata.get("retryable") is True
            and isinstance(metadata.get("error_code"), str)
            and isinstance(metadata.get("operation"), str)
        ):
            return metadata
    return classify_legacy_infrastructure_error(
        state.get("last_error"), phase=state.get("current_phase"),
    )


def readiness_is_valid(root: Path, workflow: Workflow, phase: Phase) -> bool:
    if not readiness_path(root).is_file():
        return False
    try:
        from cw.checks.deterministic import load_readiness

        manifest = load_readiness(root, phase)
        session = load_session(root, workflow, phase)
        return session is not None and manifest["session_id"] == session["session_id"]
    except CwError:
        return False


def regenerate_readiness(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    validation: ValidationResult,
) -> dict[str, Any]:
    if not validation.passed:
        raise CwError("Implemented work is not ready for recovery", ErrorCode.INVALID_STATE)
    session = create_session(root, workflow, phase)
    checks = [
        {"command": check["command"], "exit_code": check["exit_code"]}
        for check in validation.checks
        if check.get("name") == "Required command"
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session["session_id"],
        "phase": phase.id,
        "status": "READY_FOR_REVIEW",
        "artifacts": list(phase.artifacts),
        "checks_executed": checks,
    }
    atomic_json(readiness_path(root), manifest)
    return manifest


def _legacy_review_error(data: dict[str, Any]) -> Any | None:
    if data.get("reviewer_result") is None and data.get("system_error") not in (None, "", {}):
        return data["system_error"]
    return None


def _timestamp(data: dict[str, Any]) -> str:
    for key in ("created_at", "timestamp", "reviewed_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    system_error = data.get("system_error")
    if isinstance(system_error, dict):
        for key in ("timestamp", "created_at"):
            value = system_error.get(key)
            if isinstance(value, str) and value:
                return value
    return "unknown"


def migrate_legacy_reviewer_error(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    if state.get("status") != "ERROR" or not state.get("current_phase"):
        return None
    phase_id = str(state["current_phase"])
    if phase_id not in {phase.id for phase in workflow.phases}:
        return None
    migrated: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    semantic_attempts: list[int] = []
    for path in sorted((root / ".cw" / "reviews").glob(f"{phase_id}-*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = load_json(path)
        except CwError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("kind") == "semantic_review":
            attempt = data.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
                semantic_attempts.append(attempt)
            continue
        system_error = _legacy_review_error(data)
        if system_error is None:
            continue
        created_at = _timestamp(data)
        metadata = classify_legacy_infrastructure_error(
            system_error,
            phase=phase_id,
            operation="review",
            occurred_at=created_at,
        )
        if metadata is None:
            metadata = classify_legacy_infrastructure_error(
                state.get("last_error"),
                phase=phase_id,
                operation="review",
                occurred_at=created_at,
            )
        if metadata is None:
            continue
        attempt = data.get("attempt")
        normalized = {
            "schema_version": SCHEMA_VERSION,
            "workflow": workflow.id,
            "phase": phase_id,
            "attempt": attempt if isinstance(attempt, int) and attempt > 0 else int(state.get("attempt", 0)) or 1,
            "kind": "infrastructure_error",
            "error_code": metadata["error_code"],
            "error": redact(str(system_error)) or "Legacy reviewer infrastructure failure",
            "details": redact(str(state.get("last_error") or "")) or None,
            "created_at": created_at,
        }
        migrated.append((path, normalized, metadata))
    if not migrated:
        return None

    for path, normalized, _ in migrated:
        atomic_json(path, normalized)
    metadata = migrated[-1][2]
    current_attempt = state.get("attempt", 0)
    if isinstance(current_attempt, bool) or not isinstance(current_attempt, int):
        current_attempt = 0
    # Legacy prototypes counted reviewer transport invocations as semantic
    # attempts. Remove each migrated infrastructure-only record while retaining
    # any semantic reviews that are already represented on disk.
    corrected_attempt = max(0, current_attempt - len(migrated))
    state["attempt"] = max(len(semantic_attempts), corrected_attempt)
    state["infrastructure_error"] = metadata
    state.setdefault("history", []).append({
        "timestamp": utc_now(),
        "phase": phase_id,
        "action": "infrastructure_error_migrated",
        "operation": "review",
        "error_code": metadata["error_code"],
        "review": migrated[-1][0].relative_to(root).as_posix(),
    })
    record_diagnostic(
        root,
        CwError(
            "Migrated legacy reviewer infrastructure failure",
            ErrorCode(metadata["error_code"]),
            "Run: cw retry",
            details=str(state.get("last_error") or migrated[-1][1]["error"]),
        ),
        source="repair-migration",
    )
    return metadata
