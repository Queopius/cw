from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .gates import validate_gate
from .models import Workflow
from .reviews import validate_reviewer_result
from .schema import schema_version
from .utils import load_json, safe_project_path


SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
EVENT_ACTIONS = {
    "approved", "human_approved", "human_review_required", "revision_required",
    "protected_path_violation", "reopened",
}


def _files(directory: Path, label: str) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise CwError(f"{label} directory is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    files: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".json":
            raise CwError(f"Unexpected {label.lower()} entry: {entry.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        files.append(entry)
    return files


def _audit_review(path: Path, workflow: Workflow) -> dict[str, Any]:
    data = load_json(path)
    schema_version(data, f"Review {path.name}")
    phase_id = data.get("phase")
    if data.get("workflow") != workflow.id or phase_id not in {phase.id for phase in workflow.phases}:
        raise CwError(f"Review identity is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    phase = workflow.phase(str(phase_id))
    attempt = data.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise CwError(f"Review attempt is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if not isinstance(data.get("created_at"), str) or not data["created_at"]:
        raise CwError(f"Review timestamp is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    kind = data.get("kind")
    if kind == "infrastructure_error":
        if not isinstance(data.get("error_code"), str) or not isinstance(data.get("error"), str):
            raise CwError(f"Infrastructure review is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        return data
    if kind != "semantic_review":
        raise CwError(f"Review kind is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    decision, criteria, blocking_criteria, issues = validate_reviewer_result(phase, data, root=path.parents[2])
    hashes = data.get("artifact_hashes")
    if (
        decision.value != data.get("decision")
        or criteria != data.get("criteria")
        or ("blocking_criteria" in data and blocking_criteria != data.get("blocking_criteria"))
        or issues != data.get("blocking_issues")
        or not isinstance(hashes, dict)
        or set(hashes) != set(phase.artifacts)
        or any(not isinstance(value, str) or SHA256.fullmatch(value) is None for value in hashes.values())
    ):
        raise CwError(f"Semantic review is inconsistent: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def audit_history(root: Path, workflow: Workflow, state: dict[str, Any]) -> dict[str, int]:
    review_files = _files(root / ".cw" / "reviews", "Reviews")
    gate_files = _files(root / ".cw" / "gates", "Gates")
    review_references: set[str] = set()
    for path in review_files:
        _audit_review(path, workflow)
        review_references.add(path.relative_to(root).as_posix())
    expected_gate_names = {f"{phase.id}.approved.json": phase.id for phase in workflow.phases}
    gate_references: set[str] = set()
    for path in gate_files:
        phase_id = expected_gate_names.get(path.name)
        if phase_id is None:
            raise CwError(f"Gate targets an unknown phase: {path.name}", ErrorCode.INVALID_GATE)
        gate = validate_gate(root, workflow, phase_id)
        if gate.get("review_reference") not in review_references:
            raise CwError(f"Gate review reference is missing: {path.name}", ErrorCode.INVALID_GATE)
        gate_references.add(path.relative_to(root).as_posix())
    for key, known in (("last_review", review_references), ("last_gate", gate_references)):
        reference = state.get(key)
        if reference is not None:
            if not isinstance(reference, str):
                raise CwError(f"State {key} reference is invalid", ErrorCode.INVALID_STATE)
            safe_project_path(root, reference, must_exist=True)
            if reference not in known:
                raise CwError(f"State {key} reference is unknown", ErrorCode.INVALID_STATE)
    history = state.get("history")
    phase_ids = {phase.id for phase in workflow.phases}
    if not isinstance(history, list):
        raise CwError("Workflow history must be a list", ErrorCode.INVALID_STATE)
    for index, event in enumerate(history):
        if (
            not isinstance(event, dict)
            or event.get("phase") not in phase_ids
            or event.get("action") not in EVENT_ACTIONS
            or not isinstance(event.get("timestamp"), str)
            or not event["timestamp"]
        ):
            raise CwError(f"Workflow history event is invalid: {index}", ErrorCode.INVALID_STATE)
        action = event["action"]
        if action in {"approved", "human_review_required", "revision_required"}:
            attempt = event.get("attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise CwError(f"Workflow history attempt is invalid: {index}", ErrorCode.INVALID_STATE)
        if action in {"approved", "human_approved"}:
            gate = event.get("gate")
            if not isinstance(gate, str) or not gate.startswith(".cw/gates/"):
                raise CwError(f"Workflow history gate is invalid: {index}", ErrorCode.INVALID_STATE)
            gate_file = safe_project_path(root, gate)
            # Reopening deliberately removes the live gate while preserving its
            # audit event and backup. If a file remains, it must be a known gate.
            if gate_file.exists() and gate not in gate_references:
                raise CwError(f"Workflow history gate is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "revision_required" and not isinstance(event.get("issues"), list):
            raise CwError(f"Workflow history issues are invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "reopened":
            backup = event.get("backup")
            if not isinstance(backup, str) or not backup.startswith(".cw/backups/"):
                raise CwError(f"Workflow history backup is invalid: {index}", ErrorCode.INVALID_STATE)
    return {"reviews": len(review_files), "gates": len(gate_files), "events": len(history)}
