from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cw.core.completion import (
    contract_payload,
    latest_completion_review,
    load_extension_proposal,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.models import WorkflowState
from cw.core.progress import derive_effective_workflow_state
from cw.core.schema import SCHEMA_VERSION
from cw.core.session import process_is_alive
from cw.execution.processes import ProcessInspector
from cw.execution.runs import load_active_run
from cw.execution.session import load_batch

ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]


def git_branch(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "--no-pager", "branch", "--show-current"], cwd=root,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            capture_output=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return result.stdout.strip() or "detached HEAD"


def project_status(root: Path, context: ContextLoader) -> dict[str, Any]:
    """Return CW's canonical machine-readable status model."""

    project, state, workflow = context(root)
    consistency = derive_effective_workflow_state(root, workflow, state) if workflow.phases else None
    current = state.get("current_phase")
    try:
        index = workflow.index(current) if current and workflow.phases else None
    except StopIteration:
        index = None
    gates: dict[str, bool] = {}
    gate_states: dict[str, str] = {}
    invalid_gates: list[str] = []
    gate_error = None
    gate_error_code = None
    gate_error_details = None
    if consistency is not None:
        gate_states.update(consistency.chain.states)
        gates = {phase.id: gate_states.get(phase.id) == "approved" for phase in workflow.phases}
        invalid_gates = [phase.id for phase in workflow.phases if gate_states.get(phase.id) == "invalid"]
        if consistency.chain.issues:
            gate_error = "Approval gate chain is invalid"
            gate_error_code = ErrorCode.INVALID_GATE.value
            gate_error_details = "\n".join(consistency.chain.issues)
    batch = load_batch(root)
    managed_run = load_active_run(root)
    if managed_run is not None:
        process = ProcessInspector().inspect(managed_run.get("process_pid"))
        supervisor = ProcessInspector().inspect(managed_run.get("supervisor_pid"))
        managed_run = {
            **managed_run,
            "alive": process.alive or supervisor.alive,
            "stale": not (process.alive or supervisor.alive),
        }
    if (
        batch and batch.get("status") == "RUNNING"
        and (not isinstance(batch.get("pid"), int) or not process_is_alive(batch["pid"]))
    ):
        batch = {**batch, "status": "INTERRUPTED"}
    contract = workflow.completion_target
    completion_review = latest_completion_review(root) if contract is not None else None
    contract_results = completion_review.get("contract_results", []) if isinstance(completion_review, dict) else []
    verified_requirements = sum(
        isinstance(item, dict) and item.get("status") == "VERIFIED" for item in contract_results
    )
    proposal = None
    if contract is not None and state.get("extension_proposal"):
        try:
            proposal = load_extension_proposal(root, state, workflow)
        except CwError:
            proposal = None
    from cw.core.revisions import (
        active_revision,
        supersession_index,
        validation_attempts,
    )

    active_revision_id, active_revision_sha256 = active_revision(root, state, workflow) if workflow.phases else (None, None)
    supersessions = supersession_index(root) if workflow.phases else {}
    validation_attempt, revision_validation_attempt = validation_attempts(root, workflow, state) if workflow.phases else (0, 0)
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project.project_id,
        "repository_root": str(root),
        "branch": git_branch(root),
        "workflow": "INITIALIZED" if not workflow.phases else "ACTIVE",
        "plan": workflow.status,
        "state": state["status"],
        "phase": current,
        "phase_index": index,
        "position": index + 1 if index is not None else None,
        "phase_count": len(workflow.phases),
        "approved_count": consistency.approved_count if consistency is not None else 0,
        "remaining_count": consistency.remaining_count if consistency is not None else 0,
        "active_count": consistency.active_count if consistency is not None else 0,
        "effective_state": consistency.status.value if consistency is not None else state["status"],
        "is_complete": consistency.is_complete if consistency is not None else False,
        "planned_scope_complete": consistency.planned_scope_complete if consistency is not None else False,
        "completion_mode": "CONTRACT_AWARE" if contract is not None else "LEGACY",
        "completion_target": contract_payload(contract) if contract is not None else None,
        "completion_satisfied": consistency.completion_satisfied if consistency is not None else False,
        "completion_review": completion_review,
        "completion_verified_count": verified_requirements,
        "completion_requirement_count": len(contract.requirements) if contract is not None else 0,
        "extension_proposal": proposal,
        "attempt": state.get("attempt", 0),
        "revision_attempt": state.get("revision_attempt", state.get("attempt", 0)),
        "validation_attempt": validation_attempt,
        "revision_validation_attempt": revision_validation_attempt,
        "active_plan_revision": active_revision_id,
        "active_plan_revision_sha256": active_revision_sha256,
        "superseded_plan_revisions": state.get("superseded_plan_revisions", []),
        "superseded_reviews": sorted(supersessions),
        "active_review": state.get("last_review"),
        "historical_attempts": [
            {
                "phase": event.get("phase"), "attempt": event.get("attempt"),
                "outcome": event.get("action"), "timestamp": event.get("timestamp"),
            }
            for event in state.get("history", [])
            if isinstance(event, dict)
            and event.get("action") in {"approved", "revision_required", "human_review_required"}
            and isinstance(event.get("attempt"), int)
        ],
        "rebaseline": {
            "allowed": state.get("status") == WorkflowState.REVISION_REQUIRED.value
            and not (gates.get(str(current), False) if current else False),
            "pending": state.get("pending_rebaseline"),
            "completed": bool(supersessions),
        },
        "max_attempts": workflow.max_review_attempts,
        "ready": (root / ".cw/runtime/READY_FOR_REVIEW.json").is_file(),
        "gate": gates.get(current, False) if current else False,
        "gates": gates,
        "gate_states": gate_states,
        "invalid_gates": invalid_gates,
        "gate_error": gate_error,
        "gate_error_code": gate_error_code,
        "gate_error_details": gate_error_details,
        "phases": [
            {
                "id": phase.id,
                "number": phase.id.split("-", 1)[0],
                "name": phase.name,
                "objective": phase.objective,
                "depends_on": list(phase.depends_on),
            }
            for phase in workflow.phases
        ],
        "last_error": state.get("last_error"),
        "infrastructure_error": state.get("infrastructure_error"),
        "batch": batch,
        "run": managed_run,
        "consistent": consistency.consistent if consistency is not None else True,
        "consistency_issues": list(consistency.issues) if consistency is not None else [],
        "expected_phase": consistency.expected_current if consistency is not None else None,
        "approved_through": consistency.chain.approved[-1][0] if consistency and consistency.chain.approved else None,
    }


def explain_status(data: dict[str, Any]) -> dict[str, Any]:
    infrastructure = data.get("infrastructure_error")
    retry_metadata = infrastructure if isinstance(infrastructure, dict) else {}
    retryable = retry_metadata.get("retryable") is True
    return {
        "consistent": data.get("consistent", True),
        "current_phase": data.get("phase"),
        "expected_phase": data.get("expected_phase"),
        "approved_through": data.get("approved_through"),
        "issues": data.get("consistency_issues", []),
        "recovery": "cw repair" if not data.get("consistent", True) else "cw retry" if retryable else None,
        "classification": retry_metadata.get("error_code") if retryable else None,
        "failed_operation": retry_metadata.get("operation") if retryable else None,
        "retryable": retryable,
        "readiness_available": data.get("ready", False),
        "semantic_attempt": data.get("attempt", 0),
        "revision_attempt": data.get("revision_attempt", 0),
        "reason": data.get("last_error") if retryable else None,
        "planned_scope_complete": data.get("planned_scope_complete", False),
        "completion_mode": data.get("completion_mode"),
        "completion_target": data.get("completion_target"),
        "completion_satisfied": data.get("completion_satisfied", False),
        "completion_review": data.get("completion_review"),
        "extension_proposal": data.get("extension_proposal"),
        "active_plan_revision": data.get("active_plan_revision"),
        "superseded_plan_revisions": data.get("superseded_plan_revisions", []),
        "superseded_reviews": data.get("superseded_reviews", []),
        "rebaseline": data.get("rebaseline"),
        "rebaseline_explanation": (
            "The active REVISE review may be superseded only by an exact, human-authorized plan proposal. Historical evidence remains immutable."
            if data.get("rebaseline", {}).get("allowed")
            else "Rebaseline requires REVISION_REQUIRED state, an active REVISE review, no current gate, and exact human authorization."
        ),
    }
