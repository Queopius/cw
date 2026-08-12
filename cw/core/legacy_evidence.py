from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .models import Phase, Workflow
from .severity import CANONICAL_CRITERION_SEVERITIES, LEGACY_SEVERITY_ALIASES, CriterionSeverity
from .utils import safe_project_path, sha256_bytes, sha256_file


def is_legacy_review(data: Any) -> bool:
    return isinstance(data, dict) and "workflow_id" in data and "reviewer_result" in data


def is_legacy_gate(data: Any) -> bool:
    return isinstance(data, dict) and "workflow_id" in data and "review_file" in data


def _severity(value: Any, criterion_id: str) -> str:
    normalized = LEGACY_SEVERITY_ALIASES.get(value, value)
    if normalized not in CANONICAL_CRITERION_SEVERITIES:
        raise CwError(
            f"Legacy review criterion severity is invalid: {criterion_id}",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
        )
    return str(normalized)


def validate_legacy_review(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    data: dict[str, Any],
    *,
    require_approval: bool = False,
) -> dict[str, Any]:
    if (
        data.get("workflow_id") != workflow.id
        or data.get("phase") != phase.id
        or not isinstance(data.get("timestamp"), str)
        or not data["timestamp"]
        or isinstance(data.get("attempt"), bool)
        or not isinstance(data.get("attempt"), int)
        or data["attempt"] < 1
    ):
        raise CwError(f"Legacy review identity is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)

    reviewer = data.get("reviewer_result")
    final_decision = data.get("final_decision")
    if reviewer is None:
        if final_decision != "ERROR" or data.get("system_error") in (None, "", {}):
            raise CwError(f"Legacy infrastructure review is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        return data
    if not isinstance(reviewer, dict):
        raise CwError(f"Legacy reviewer result is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    decision = reviewer.get("decision")
    human_gate_after_technical_approval = decision == "APPROVE" and final_decision == "HUMAN_REVIEW_REQUIRED"
    if (
        decision not in {"APPROVE", "REVISE", "HUMAN_REVIEW_REQUIRED"}
        or (final_decision != decision and not human_gate_after_technical_approval)
    ):
        raise CwError(f"Legacy reviewer decision is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)

    configured = {criterion.id: criterion for criterion in phase.acceptance_criteria}
    received: dict[str, dict[str, Any]] = {}
    criteria = reviewer.get("criteria")
    if not isinstance(criteria, list):
        raise CwError(f"Legacy review criteria are invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    for result in criteria:
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise CwError(f"Legacy review criterion is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        criterion_id = result["id"]
        if criterion_id in received or criterion_id not in configured:
            raise CwError(f"Legacy review criterion set is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        evidence = result.get("evidence")
        if (
            not isinstance(result.get("passed"), bool)
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item.strip() for item in evidence)
            or _severity(result.get("severity"), criterion_id) != configured[criterion_id].severity.value
        ):
            raise CwError(f"Legacy review criterion is inconsistent: {criterion_id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        received[criterion_id] = result
    if set(received) != set(configured):
        raise CwError(f"Legacy review criteria are incomplete: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)

    blocking_issues = reviewer.get("blocking_issues")
    if not isinstance(blocking_issues, list):
        raise CwError(f"Legacy review blocking issues are invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    for issue in blocking_issues:
        if isinstance(issue, str) and issue.strip():
            continue
        if (
            not isinstance(issue, dict)
            or set(issue) != {"criterion_id", "description", "required_change"}
            or issue.get("criterion_id") not in configured
            or not all(
                isinstance(issue.get(key), str) and issue[key].strip()
                for key in ("description", "required_change")
            )
        ):
            raise CwError(f"Legacy review blocking issues are invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    blocking_failed = any(
        criterion.severity == CriterionSeverity.BLOCKING and received[criterion.id]["passed"] is False
        for criterion in phase.acceptance_criteria
    )
    if decision == "APPROVE" and (
        blocking_failed
        or blocking_issues
        or reviewer.get("next_phase_allowed") is not True
    ):
        raise CwError(f"Legacy approval review is inconsistent: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if require_approval and decision != "APPROVE":
        raise CwError(f"Legacy gate review is not approved: {phase.id}", ErrorCode.INVALID_GATE)

    hashes = data.get("artifact_hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(phase.artifacts):
        raise CwError(f"Legacy review artifact hashes are invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def validate_legacy_gate(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    data: dict[str, Any],
) -> dict[str, Any]:
    if (
        data.get("workflow_id") != workflow.id
        or data.get("workflow_version") != workflow.version
        or data.get("phase") != phase.id
        or data.get("decision") != "APPROVED"
        or data.get("approval_type") not in {"independent-review", "human"}
        or not isinstance(data.get("approved_at"), str)
    ):
        raise CwError(f"Invalid legacy approval gate: {phase.id}", ErrorCode.INVALID_GATE)
    reference = data.get("review_file")
    if not isinstance(reference, str) or not reference.startswith(".cw/reviews/"):
        raise CwError(f"Legacy gate has an invalid review reference: {phase.id}", ErrorCode.INVALID_GATE)
    review_path = safe_project_path(root, reference, must_exist=True)
    if review_path.parent != root / ".cw" / "reviews" or not review_path.is_file() or review_path.is_symlink():
        raise CwError(f"Legacy gate has an invalid review reference: {phase.id}", ErrorCode.INVALID_GATE)
    from .utils import load_json

    review = load_json(review_path)
    expected_review_hash = data.get("review_sha256")
    current_hash = sha256_file(review_path)
    if current_hash != expected_review_hash and isinstance(review, dict):
        # v0.1 schema migration appended only these two metadata fields. Verify
        # the exact pre-migration serialization so an original gate hash remains
        # authoritative without rewriting either historical document.
        pre_schema = dict(review)
        pre_schema.pop("schema_version", None)
        pre_schema.pop("cw_version", None)
        rendered = json.dumps(pre_schema, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
        current_hash = sha256_bytes(rendered.encode("utf-8"))
    if not isinstance(expected_review_hash, str) or current_hash != expected_review_hash:
        raise CwError(f"Legacy gate review hash is invalid: {phase.id}", ErrorCode.INVALID_GATE)
    if not is_legacy_review(review):
        raise CwError(f"Legacy gate review format is invalid: {phase.id}", ErrorCode.INVALID_GATE)
    try:
        validate_legacy_review(root, workflow, phase, review, require_approval=True)
    except CwError as exc:
        raise CwError(f"Legacy gate review evidence is invalid: {phase.id}", ErrorCode.INVALID_GATE) from exc
    expected = data.get("artifacts")
    if expected != review.get("artifact_hashes") or not isinstance(expected, dict) or set(expected) != set(phase.artifacts):
        raise CwError(f"Legacy gate artifact hashes are invalid: {phase.id}", ErrorCode.INVALID_GATE)
    from .gates import artifact_hashes

    if artifact_hashes(root, phase.artifacts) != expected:
        raise CwError("Approval gate invalidated", ErrorCode.INVALID_GATE, details=f"Phase: {phase.id}")
    return data
