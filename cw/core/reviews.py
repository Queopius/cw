from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .models import Phase, ReviewDecision
from .severity import CriterionSeverity
from .utils import safe_project_path


_EVIDENCE_REFERENCE = re.compile(r"^(?P<path>.+?)(?::(?P<line>[1-9][0-9]*(?:-[1-9][0-9]*)?))?$")


def _evidence_path(root: Path, phase: Phase, value: str) -> bool:
    token = value.strip().split(maxsplit=1)[0]
    match = _EVIDENCE_REFERENCE.fullmatch(token)
    if match is None:
        return False
    relative = match.group("path").replace("\\", "/").removeprefix("./")
    try:
        path = safe_project_path(root, relative, must_exist=True)
    except CwError:
        return False
    if not path.is_file() or path.is_symlink():
        return False
    patterns = (*phase.artifacts, *phase.review_paths)
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").removeprefix("./")
        if relative == normalized or fnmatch.fnmatchcase(relative, normalized):
            return True
        if "/**/" in normalized:
            prefix = normalized.split("/**/", 1)[0].rstrip("/")
            if relative.startswith(f"{prefix}/"):
                return True
    return False


def validate_reviewer_result(
    phase: Phase,
    payload: dict[str, Any],
    *,
    require_blocking_criteria: bool = False,
    strict: bool = False,
    root: Path | None = None,
) -> tuple[ReviewDecision, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    allowed = {"decision", "summary", "blocking_issues", "criteria", "blocking_criteria"}
    if strict and set(payload) != allowed:
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    try:
        decision = ReviewDecision(str(payload["decision"]))
        results = payload["criteria"]
        issues = payload.get("blocking_issues", [])
    except (KeyError, ValueError, TypeError) as exc:
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry") from exc
    summary = payload.get("summary")
    if (strict or "summary" in payload) and (not isinstance(summary, str) or not summary.strip()):
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    if (
        not isinstance(results, list)
        or not isinstance(issues, list)
        or not all(isinstance(v, str) and v.strip() for v in issues)
    ):
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    expected = {criterion.id: criterion for criterion in phase.acceptance_criteria}
    received: dict[str, dict[str, Any]] = {}
    consistency: list[str] = []
    for result in results:
        result_fields = set(result) if isinstance(result, dict) else set()
        if (
            not isinstance(result, dict)
            or (
                result_fields != {"id", "status", "evidence"}
                and (strict or result_fields != {"id", "status", "evidence", "severity"})
            )
            or not isinstance(result.get("id"), str)
        ):
            consistency.append("Malformed criterion result")
            continue
        criterion_id = result["id"]
        if criterion_id in received:
            consistency.append(f"Duplicate criterion: {criterion_id}")
        elif criterion_id not in expected:
            consistency.append(f"Invented criterion: {criterion_id}")
        else:
            received[criterion_id] = result
            configured_severity = expected[criterion_id].severity.value
            if "severity" in result and result["severity"] != configured_severity:
                consistency.append(f"Criterion severity mismatch: {criterion_id}")
    for criterion_id in expected:
        if criterion_id not in received:
            consistency.append(f"Missing criterion: {criterion_id}")
    valid_status = {"PASS", "FAIL", "UNKNOWN"}
    validate_paths = strict or "blocking_criteria" in payload
    for criterion_id, result in received.items():
        evidence = result.get("evidence")
        if (
            result.get("status") not in valid_status
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item.strip() for item in evidence)
        ):
            consistency.append(f"Insufficient result evidence: {criterion_id}")
        elif validate_paths and (root is None or not all(_evidence_path(root, phase, item) for item in evidence)):
            consistency.append(f"Evidence is outside review scope: {criterion_id}")

    blocking_results = payload.get("blocking_criteria")
    if blocking_results is None and not require_blocking_criteria:
        blocking_results = []
    if not isinstance(blocking_results, list):
        raise CwError("Reviewer blocking criteria are missing", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    expected_blocking = set(phase.blocking_criteria)
    received_blocking: dict[str, dict[str, Any]] = {}
    for result in blocking_results:
        if (
            not isinstance(result, dict)
            or set(result) != {"description", "status", "evidence"}
            or not isinstance(result.get("description"), str)
        ):
            consistency.append("Malformed blocking criterion result")
            continue
        description = result["description"]
        if description in received_blocking:
            consistency.append(f"Duplicate blocking criterion: {description}")
        elif description not in expected_blocking:
            consistency.append(f"Invented blocking criterion: {description}")
        else:
            received_blocking[description] = result
    if require_blocking_criteria or "blocking_criteria" in payload:
        for description in phase.blocking_criteria:
            if description not in received_blocking:
                consistency.append(f"Missing blocking criterion: {description}")
    for description, result in received_blocking.items():
        evidence = result.get("evidence")
        if (
            result.get("status") not in valid_status
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item.strip() for item in evidence)
        ):
            consistency.append(f"Insufficient blocking evidence: {description}")
        elif validate_paths and (root is None or not all(_evidence_path(root, phase, item) for item in evidence)):
            consistency.append(f"Blocking evidence is outside review scope: {description}")
    blocking_failed = [
        criterion_id for criterion_id, criterion in expected.items()
        if criterion.severity == CriterionSeverity.BLOCKING
        and received.get(criterion_id, {}).get("status") != "PASS"
    ]
    configured_blockers = [
        description for description in phase.blocking_criteria
        if received_blocking.get(description, {}).get("status") != "PASS"
    ] if require_blocking_criteria or "blocking_criteria" in payload else []
    if consistency or blocking_failed or configured_blockers or issues:
        if decision is ReviewDecision.APPROVE:
            decision = ReviewDecision.REVISE
        issues = [*issues, *blocking_failed, *configured_blockers, *consistency]
    elif decision is ReviewDecision.REVISE:
        issues.append("Reviewer requested revision without a blocking criterion failure")
    return decision, list(received.values()), list(received_blocking.values()), list(dict.fromkeys(issues))
