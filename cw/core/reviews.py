from __future__ import annotations

from typing import Any

from .errors import CwError, ErrorCode
from .models import Phase, ReviewDecision


def validate_reviewer_result(
    phase: Phase,
    payload: dict[str, Any],
) -> tuple[ReviewDecision, list[dict[str, Any]], list[str]]:
    try:
        decision = ReviewDecision(str(payload["decision"]))
        results = payload["criteria"]
        issues = payload.get("blocking_issues", [])
    except (KeyError, ValueError, TypeError) as exc:
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry") from exc
    if not isinstance(results, list) or not isinstance(issues, list) or not all(isinstance(v, str) for v in issues):
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    expected = {criterion.id: criterion for criterion in phase.acceptance_criteria}
    received: dict[str, dict[str, Any]] = {}
    consistency: list[str] = []
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            consistency.append("Malformed criterion result")
            continue
        criterion_id = result["id"]
        if criterion_id in received:
            consistency.append(f"Duplicate criterion: {criterion_id}")
        elif criterion_id not in expected:
            consistency.append(f"Invented criterion: {criterion_id}")
        else:
            received[criterion_id] = result
    for criterion_id in expected:
        if criterion_id not in received:
            consistency.append(f"Missing criterion: {criterion_id}")
    valid_status = {"PASS", "FAIL", "UNKNOWN"}
    for criterion_id, result in received.items():
        if result.get("status") not in valid_status or not isinstance(result.get("evidence"), list) or not result["evidence"]:
            consistency.append(f"Insufficient result evidence: {criterion_id}")
    blocking_failed = [
        criterion_id for criterion_id, criterion in expected.items()
        if criterion.severity == "blocking" and received.get(criterion_id, {}).get("status") != "PASS"
    ]
    if consistency or blocking_failed or issues:
        if decision is ReviewDecision.APPROVE:
            decision = ReviewDecision.REVISE
        issues = [*issues, *blocking_failed, *consistency]
    elif decision is ReviewDecision.REVISE:
        issues.append("Reviewer requested revision without a blocking criterion failure")
    return decision, list(received.values()), list(dict.fromkeys(issues))
