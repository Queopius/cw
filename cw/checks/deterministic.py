from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cw.checks.verification import VerificationExecutor
from cw.core.errors import CwError, ErrorCode
from cw.core.models import Phase, ValidationResult, Workflow
from cw.core.schema import schema_version
from cw.core.session import load_session, readiness_path
from cw.core.utils import atomic_json, load_json, safe_project_path


def load_readiness(root: Path, phase: Phase) -> dict[str, Any]:
    path = readiness_path(root)
    if not path.is_file() or path.is_symlink():
        raise CwError("Readiness manifest is missing", ErrorCode.SCHEMA_VALIDATION_ERROR, "Complete the phase and create .cw/runtime/READY_FOR_REVIEW.json")
    data = load_json(path)
    if not isinstance(data, dict):
        raise CwError("Readiness manifest must be an object", ErrorCode.SCHEMA_VALIDATION_ERROR)
    schema_version(data, "Readiness manifest")
    allowed = {"schema_version", "session_id", "phase", "status", "artifacts", "checks_executed", "verification_receipt"}
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
    receipt = data.get("verification_receipt")
    if receipt is not None and (
        not isinstance(receipt, dict)
        or set(receipt) != {"reference", "sha256", "digest", "receipt_id"}
        or not all(isinstance(value, str) and value for value in receipt.values())
    ):
        raise CwError("Readiness verification receipt reference is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def inspect_completed_work(root: Path, workflow: Workflow, phase: Phase) -> ValidationResult:
    """Validate implemented work without trusting or requiring a readiness manifest."""
    return VerificationExecutor().execute(root, workflow, phase)


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
        # Persisted receipts are evidence only. Regenerate verification unless
        # an independent in-memory expectation is available.
        # Establish existence before commands, recheck dependency gates after
        # commands, and bind final file bytes into the semantic review.
        verified = VerificationExecutor().execute(root, workflow, phase)
        result.checks.extend(verified.checks)
        result.artifact_hashes = verified.artifact_hashes
        result.error_code = verified.error_code
        result.receipt = verified.receipt
        result.receipt_payload = verified.receipt_payload
        result.passed = verified.passed
        if not verified.passed:
            result.errors.extend(verified.errors)
        elif verified.receipt is not None:
            manifest["verification_receipt"] = verified.receipt
            atomic_json(readiness_path(root), manifest)
    except CwError as exc:
        message = str(exc)
        result.errors.append(message)
        result.checks.append({"name": "Validation", "status": "failed", "detail": message})
    return result
