from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from cw.adapters.codex import CodexAdapter
from cw.checks.deterministic import validate_phase
from cw.core.errors import CwError, ErrorCode, HumanActionRequired
from cw.core.gates import artifact_hashes, create_gate
from cw.core.models import Phase, ReviewDecision, Workflow, WorkflowState
from cw.core.reviews import validate_reviewer_result
from cw.core.session import finish_session, readiness_path
from cw.core.state import save_state, transition
from cw.core.utils import atomic_json, load_json, utc_now


def reviewer_prompt(workflow: Workflow, phase: Phase) -> str:
    criteria = [{"id": item.id, "description": item.description, "severity": item.severity} for item in phase.acceptance_criteria]
    return f"""You are the independent CW reviewer. Remain strictly read-only.
Review ONLY phase {phase.id}: {phase.name}.
Objective: {phase.objective}
Allowed review paths: {json.dumps(phase.review_paths)}
Artifacts: {json.dumps(phase.artifacts)}
Acceptance criteria: {json.dumps(criteria)}

Evaluate every listed criterion exactly once. Cite concrete repository evidence.
Ambiguous or missing evidence is not a pass. Do not invent criteria and do not review future phases.
Return only the JSON object required by the supplied schema.
"""


def _event(state: dict[str, Any], phase: str, action: str, **extra: Any) -> None:
    state.setdefault("history", []).append({"timestamp": utc_now(), "phase": phase, "action": action, **extra})


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
    schema = Path(__file__).resolve().parents[1] / "schemas" / "phase-review.schema.json"
    try:
        response = reviewer.run_reviewer(root, reviewer_prompt(workflow, phase), schema, workflow.review_timeout)
        decision, criteria, issues = validate_reviewer_result(phase, response.payload)
    except CwError as exc:
        state["last_error"] = f"{exc.code.value}: {exc.message}\n{exc.details or ''}".rstrip()
        transition(root, state, WorkflowState.ERROR, force_error=True)
        report = {
            "schema_version": 1, "workflow": workflow.id, "phase": phase.id,
            "attempt": attempt, "kind": "infrastructure_error", "error_code": exc.code.value,
            "error": exc.message, "details": exc.details, "created_at": utc_now(),
        }
        path = root / ".cw" / "reviews" / f"{phase.id}-infrastructure-{utc_now().replace(':', '')}-{secrets.token_hex(4)}.json"
        atomic_json(path, report)
        state["last_review"] = path.relative_to(root).as_posix()
        save_state(root, state)
        raise

    state["attempt"] = attempt
    report = {
        "schema_version": 1, "workflow": workflow.id, "phase": phase.id, "attempt": attempt,
        "kind": "semantic_review", "decision": decision.value, "criteria": criteria,
        "blocking_issues": issues, "artifact_hashes": validation.artifact_hashes, "created_at": utc_now(),
    }
    path = root / ".cw" / "reviews" / f"{phase.id}-attempt-{attempt:02d}.json"
    atomic_json(path, report)
    state["last_review"] = path.relative_to(root).as_posix()
    state["last_error"] = None

    if decision is ReviewDecision.APPROVE:
        if phase.requires_human_approval:
            _event(state, phase.id, "human_review_required", attempt=attempt)
            transition(root, state, WorkflowState.HUMAN_REVIEW_REQUIRED)
            readiness_path(root).unlink(missing_ok=True)
            finish_session(root)
            return report
        gate = create_gate(root, workflow, phase, state["last_review"])
        state["last_gate"] = gate.relative_to(root).as_posix()
        _event(state, phase.id, "approved", attempt=attempt, gate=state["last_gate"])
        transition(root, state, WorkflowState.APPROVED)
        readiness_path(root).unlink(missing_ok=True)
        finish_session(root)
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
    review_path = root / str(state["last_review"])
    review = load_json(review_path)
    expected = review.get("artifact_hashes") if isinstance(review, dict) else None
    current = artifact_hashes(root, phase.artifacts)
    if not isinstance(expected, dict) or expected != current:
        raise CwError("Artifacts changed after semantic review", ErrorCode.INVALID_GATE, "Reopen and review the phase again.")
    gate = create_gate(root, workflow, phase, str(state["last_review"]), human_approved=True)
    state["last_gate"] = gate.relative_to(root).as_posix()
    _event(state, phase.id, "human_approved", gate=state["last_gate"])
    transition(root, state, WorkflowState.APPROVED)
    readiness_path(root).unlink(missing_ok=True)
    finish_session(root)
    return gate
