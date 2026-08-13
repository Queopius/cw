from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .gates import gate_path, validate_gate
from .models import Workflow, WorkflowState
from .session import finish_session, readiness_path
from .workflow import load_workflow, set_plan_status


def valid_gate_ids(root: Path, workflow: Workflow) -> list[str]:
    """Return verified gates in configured order, failing closed on corruption."""
    approved: list[str] = []
    for phase in workflow.phases:
        if gate_path(root, phase.id).is_file():
            validate_gate(root, workflow, phase.id)
            approved.append(phase.id)
    return approved


def normalize_legacy_progress(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
) -> tuple[Workflow, bool]:
    """Recover executable plan/phase state from authoritative legacy gates.

    A plan is never promoted from PROPOSED without verified approval evidence.
    Existing gates must describe a contiguous executed prefix; anything else is
    ambiguous and therefore rejected.
    """
    approved = valid_gate_ids(root, workflow)
    if not approved:
        return workflow, False

    approved_set = set(approved)
    prefix: list[str] = []
    pending_seen = False
    for phase in workflow.phases:
        if phase.id in approved_set:
            if pending_seen:
                raise CwError(
                    "Approval gates do not form an executable phase sequence",
                    ErrorCode.INVALID_GATE,
                    "Inspect gates and reopen the first affected phase.",
                )
            prefix.append(phase.id)
        else:
            pending_seen = True

    changed = False
    if workflow.status == "PROPOSED":
        set_plan_status(root, "APPROVED")
        workflow = load_workflow(root)
        changed = True

    current = state.get("current_phase")
    status = str(state.get("status"))
    approved_current = current in approved_set
    legacy_executable = status in {
        WorkflowState.PLAN_PROPOSED.value,
        WorkflowState.READY.value,
        WorkflowState.APPROVED.value,
    }
    if approved_current or legacy_executable:
        latest = workflow.phase(prefix[-1])
        latest_gate = validate_gate(root, workflow, latest.id)
        state["last_gate"] = gate_path(root, latest.id).relative_to(root).as_posix()
        reference = latest_gate.get("review_reference") or latest_gate.get("review_file")
        if isinstance(reference, str):
            state["last_review"] = reference
        state["last_error"] = None
        state["infrastructure_error"] = None
        if len(prefix) == len(workflow.phases):
            state["current_phase"] = workflow.phases[-1].id
            state["status"] = WorkflowState.COMPLETED.value
        else:
            state["current_phase"] = workflow.phases[len(prefix)].id
            state["status"] = WorkflowState.IN_PROGRESS.value
            state["attempt"] = 0
        readiness_path(root).unlink(missing_ok=True)
        finish_session(root)
        changed = True
    return workflow, changed
