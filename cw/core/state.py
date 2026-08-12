from __future__ import annotations

from pathlib import Path
from typing import Any

from cw import __version__
from .errors import CwError, ErrorCode
from .models import Workflow, WorkflowState
from .utils import atomic_json, load_json, utc_now
from .workflow import workflow_hash


TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.UNINITIALIZED: {WorkflowState.PLANNING},
    WorkflowState.PLANNING: {WorkflowState.PLAN_PROPOSED, WorkflowState.ERROR},
    WorkflowState.PLAN_PROPOSED: {WorkflowState.READY, WorkflowState.PLANNING},
    WorkflowState.READY: {WorkflowState.IN_PROGRESS},
    WorkflowState.IN_PROGRESS: {WorkflowState.READY_FOR_REVIEW, WorkflowState.ERROR, WorkflowState.PAUSED},
    WorkflowState.READY_FOR_REVIEW: {WorkflowState.REVIEWING, WorkflowState.IN_PROGRESS, WorkflowState.ERROR},
    WorkflowState.REVIEWING: {WorkflowState.APPROVED, WorkflowState.REVISION_REQUIRED, WorkflowState.HUMAN_REVIEW_REQUIRED, WorkflowState.ERROR},
    WorkflowState.REVISION_REQUIRED: {WorkflowState.IN_PROGRESS, WorkflowState.ERROR},
    WorkflowState.APPROVED: {WorkflowState.IN_PROGRESS, WorkflowState.COMPLETED},
    WorkflowState.HUMAN_REVIEW_REQUIRED: {WorkflowState.APPROVED, WorkflowState.IN_PROGRESS},
    WorkflowState.ERROR: {WorkflowState.IN_PROGRESS, WorkflowState.READY_FOR_REVIEW, WorkflowState.PLANNING},
    WorkflowState.PAUSED: {WorkflowState.IN_PROGRESS},
    WorkflowState.COMPLETED: set(),
}


def initial_state(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "cw_version": __version__, "workflow_id": project_id,
        "workflow_version": None, "workflow_sha256": None, "current_phase": None,
        "status": WorkflowState.UNINITIALIZED.value, "attempt": 0,
        "last_review": None, "last_gate": None, "last_error": None,
        "history": [], "updated_at": utc_now(),
    }


def load_state(root: Path) -> dict[str, Any]:
    data = load_json(root / ".cw" / "state.json")
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise CwError("Workflow state schema is incompatible", ErrorCode.INVALID_STATE, "Run: cw repair")
    try:
        WorkflowState(str(data.get("status")))
    except ValueError as exc:
        raise CwError("Workflow state is invalid", ErrorCode.INVALID_STATE, "Run: cw repair") from exc
    return data


def save_state(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(root / ".cw" / "state.json", state)


def transition(root: Path, state: dict[str, Any], target: WorkflowState, *, force_error: bool = False) -> None:
    current = WorkflowState(state["status"])
    if target not in TRANSITIONS[current] and not (force_error and target is WorkflowState.ERROR):
        raise CwError(f"Invalid state transition: {current.value} → {target.value}", ErrorCode.INVALID_STATE)
    state["status"] = target.value
    save_state(root, state)


def bind_plan(root: Path, state: dict[str, Any], workflow: Workflow) -> None:
    path = root / ".codex" / "workflow" / "phases.yaml"
    state.update({
        "workflow_id": workflow.id, "workflow_version": workflow.version,
        "workflow_sha256": workflow_hash(path),
        "current_phase": workflow.phases[0].id if workflow.phases else None,
        "attempt": 0, "last_review": None, "last_gate": None, "last_error": None,
    })
    save_state(root, state)


def validate_state(root: Path, state: dict[str, Any], workflow: Workflow) -> None:
    if state.get("workflow_id") != workflow.id:
        raise CwError("Workflow state belongs to another project", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair")
    if state.get("workflow_sha256") != workflow_hash(root / ".codex" / "workflow" / "phases.yaml"):
        raise CwError("Workflow changed after state was created", ErrorCode.INVALID_STATE, "Run: cw plan rebuild")
    if workflow.phases and state.get("current_phase") not in {phase.id for phase in workflow.phases}:
        raise CwError("Current phase is not in the workflow", ErrorCode.INVALID_STATE)
