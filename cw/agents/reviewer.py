from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from cw.adapters.codex import CodexAdapter
from cw.adapters.structured_output import codex_schema
from cw.checks.deterministic import validate_phase
from cw.core.diagnostics import redact, state_error
from cw.core.errors import CwError, ErrorCode, HumanActionRequired
from cw.core.gates import artifact_hashes, create_gate, validate_approval_review
from cw.core.models import Phase, ReviewDecision, Workflow, WorkflowState
from cw.core.recovery import mark_infrastructure_error
from cw.core.reviews import validate_reviewer_result
from cw.core.schema import SCHEMA_VERSION
from cw.core.severity import CriterionSeverity
from cw.core.session import finish_session, readiness_path
from cw.core.state import advance_after_approval, save_state, transition
from cw.core.utils import atomic_json_new, utc_now


def reviewer_prompt(workflow: Workflow, phase: Phase) -> str:
    criteria = [{"id": item.id, "description": item.description, "severity": item.severity} for item in phase.acceptance_criteria]
    return f"""You are the independent CW reviewer. Remain strictly read-only.
Review ONLY phase {phase.id}: {phase.name}.
Objective: {phase.objective}
Allowed review paths: {json.dumps(phase.review_paths)}
Artifacts: {json.dumps(phase.artifacts)}
Acceptance criteria: {json.dumps(criteria)}
Blocking criteria: {json.dumps(phase.blocking_criteria)}

Evidence entries must begin with an allowed project-relative file path and may
include a line suffix, for example `src/service.py:42 concrete observation`.
Evaluate every acceptance and blocking criterion exactly once. A blocking
criterion passes only when concrete evidence proves that condition is absent.
An advisory acceptance failure is an observation, not a blocking issue, and
must not change an otherwise valid APPROVE decision to REVISE.
Cite concrete repository evidence.
Ambiguous or missing evidence is not a pass. Do not invent criteria and do not review future phases.
Return only the JSON object required by the supplied schema.
"""


def _event(state: dict[str, Any], phase: str, action: str, **extra: Any) -> None:
    state.setdefault("history", []).append({"timestamp": utc_now(), "phase": phase, "action": action, **extra})


def _persist_review(root: Path, phase: Phase, report: dict[str, Any], label: str) -> Path:
    timestamp = str(report["created_at"]).replace(":", "").replace("-", "")
    directory = root / ".cw" / "reviews"
    for _ in range(10):
        path = directory / f"{phase.id}-{label}-{timestamp}-{secrets.token_hex(8)}.json"
        try:
            atomic_json_new(path, report)
        except FileExistsError:
            continue
        return path
    raise CwError("Could not allocate an append-only review record", ErrorCode.WORKFLOW_ERROR)


def run_review(root: Path, workflow: Workflow, phase: Phase, state: dict[str, Any], adapter: CodexAdapter | None = None) -> dict[str, Any]:
    validation = validate_phase(root, workflow, phase)
    if not validation.passed:
        raise CwError("Deterministic validation failed", ErrorCode.WORKFLOW_ERROR, "Run: cw validate", details="\n".join(validation.errors))
    current = WorkflowState(state["status"])
    if current is WorkflowState.REVISION_REQUIRED:
        transition(root, state, WorkflowState.IN_PROGRESS)
        current = WorkflowState.IN_PROGRESS
    if current is WorkflowState.IN_PROGRESS:
        transition(root, state, WorkflowState.READY_FOR_REVIEW)
    elif current is WorkflowState.ERROR:
        transition(root, state, WorkflowState.READY_FOR_REVIEW)
    if WorkflowState(state["status"]) is WorkflowState.READY_FOR_REVIEW:
        transition(root, state, WorkflowState.REVIEWING)
    elif WorkflowState(state["status"]) is not WorkflowState.REVIEWING:
        raise CwError("Phase is not ready for review", ErrorCode.INVALID_STATE)

    attempt = int(state.get("attempt", 0)) + 1
    reviewer = adapter or CodexAdapter()
    schema = codex_schema("review-output.schema.json")
    try:
        response = reviewer.run_reviewer(root, reviewer_prompt(workflow, phase), schema, workflow.review_timeout)
        decision, criteria, blocking_criteria, issues = validate_reviewer_result(
            phase, response.payload, require_blocking_criteria=True, strict=True, root=root,
        )
    except CwError as exc:
        state["last_error"] = state_error(exc)
        metadata = mark_infrastructure_error(
            state, exc, operation="review", phase=phase.id,
        )
        _event(
            state, phase.id, "infrastructure_error",
            operation="review", error_code=metadata["error_code"],
        )
        transition(root, state, WorkflowState.ERROR, force_error=True)
        report = {
            "schema_version": SCHEMA_VERSION, "workflow": workflow.id, "phase": phase.id,
            "attempt": attempt, "kind": "infrastructure_error", "error_code": exc.code.value,
            "error": redact(exc.message), "details": redact(exc.details), "created_at": utc_now(),
        }
        path = _persist_review(root, phase, report, "infrastructure")
        state["last_review"] = path.relative_to(root).as_posix()
        save_state(root, state)
        raise

    state["attempt"] = attempt
    configured = {criterion.id: criterion for criterion in phase.acceptance_criteria}
    criteria = [
        {**criterion, "severity": configured[criterion["id"]].severity.value}
        for criterion in criteria
    ]
    report = {
        "schema_version": SCHEMA_VERSION, "workflow": workflow.id, "phase": phase.id, "attempt": attempt,
        "kind": "semantic_review", "decision": decision.value, "summary": response.payload["summary"],
        "criteria": criteria, "blocking_criteria": blocking_criteria,
        "blocking_issues": issues, "artifact_hashes": validation.artifact_hashes, "created_at": utc_now(),
    }
    path = _persist_review(root, phase, report, f"attempt-{attempt:02d}")
    state["last_review"] = path.relative_to(root).as_posix()
    state["last_error"] = None
    state["infrastructure_error"] = None

    if decision is ReviewDecision.APPROVE:
        if phase.requires_human_approval:
            _event(state, phase.id, "human_review_required", attempt=attempt)
            transition(root, state, WorkflowState.HUMAN_REVIEW_REQUIRED)
            readiness_path(root).unlink(missing_ok=True)
            finish_session(root)
            return report
        gate = create_gate(root, workflow, phase, state["last_review"])
        gate_reference = gate.relative_to(root).as_posix()
        next_phase = advance_after_approval(
            root, state, workflow, phase, gate_reference, attempt=attempt,
        )
        report = {
            **report,
            "gate": gate_reference,
            "next_phase": next_phase.id if next_phase else None,
            "workflow_completed": next_phase is None,
        }
    elif decision is ReviewDecision.HUMAN_REVIEW_REQUIRED:
        _event(state, phase.id, "human_review_required", attempt=attempt)
        transition(root, state, WorkflowState.HUMAN_REVIEW_REQUIRED)
        readiness_path(root).unlink(missing_ok=True)
        finish_session(root)
    else:
        _event(state, phase.id, "revision_required", attempt=attempt, issues=issues)
        transition(root, state, WorkflowState.REVISION_REQUIRED)
        readiness_path(root).unlink(missing_ok=True)
        finish_session(root)
        if attempt >= workflow.max_review_attempts:
            raise HumanActionRequired("Maximum semantic review attempts reached", hint="Inspect cw history and revise the plan or implementation.")
    return report


def human_approve(root: Path, workflow: Workflow, phase: Phase, state: dict[str, Any]) -> Path:
    if WorkflowState(state["status"]) is not WorkflowState.HUMAN_REVIEW_REQUIRED:
        raise CwError("No human approval is pending", ErrorCode.INVALID_STATE)
    if not state.get("last_review"):
        raise CwError("Human approval has no review reference", ErrorCode.INVALID_STATE)
    review = validate_approval_review(root, workflow, phase, str(state["last_review"]))
    expected = review["artifact_hashes"]
    current = artifact_hashes(root, phase.artifacts)
    if not isinstance(expected, dict) or expected != current:
        raise CwError("Artifacts changed after semantic review", ErrorCode.INVALID_GATE, "Reopen and review the phase again.")
    gate = create_gate(root, workflow, phase, str(state["last_review"]), human_approved=True)
    gate_reference = gate.relative_to(root).as_posix()
    advance_after_approval(
        root,
        state,
        workflow,
        phase,
        gate_reference,
        attempt=int(review["attempt"]),
        action="human_approved",
    )
    return gate
