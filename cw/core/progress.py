from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .gates import gate_path, validate_gate, validate_dependencies
from .models import Workflow, WorkflowState
from .reviews import validate_reviewer_result
from .session import finish_session, readiness_path
from .workflow import load_workflow, set_plan_status


_RESOLVED_CW_METADATA_ERROR_SIGNATURES = (
    "Changed or removed: .cw/project.json",
    "Protected workflow state changed without a review",
)


def resolved_cw_metadata_error(state: dict[str, Any]) -> bool:
    """Recognize narrowly scoped historical false positives owned by CW."""
    error = state.get("last_error")
    return (
        state.get("status") == WorkflowState.ERROR.value
        and state.get("infrastructure_error") is None
        and isinstance(error, str)
        and error.startswith("PROTECTED_PATH_MODIFIED:")
        and any(signature in error for signature in _RESOLVED_CW_METADATA_ERROR_SIGNATURES)
    )


@dataclass(frozen=True, slots=True)
class GateChain:
    approved: tuple[tuple[str, dict[str, Any]], ...]
    states: dict[str, str]
    issues: tuple[str, ...]
    first_broken_phase: str | None


@dataclass(frozen=True, slots=True)
class EffectiveWorkflowState:
    """Canonical workflow position derived from validated approval evidence.

    Persisted state is compared with this result, but it never determines the
    approved prefix, completion, counters, or next executable phase.
    """

    chain: GateChain
    status: WorkflowState
    current_phase: str | None
    approved_phases: tuple[str, ...]
    approved_count: int
    remaining_count: int
    active_count: int
    last_gate: str | None
    last_review: str | None
    is_complete: bool
    planned_scope_complete: bool
    completion_mode: str
    completion_satisfied: bool
    consistent: bool
    issues: tuple[str, ...]

    @property
    def expected_current(self) -> str | None:
        return self.current_phase

    @property
    def expected_last_gate(self) -> str | None:
        return self.last_gate


def derive_gate_chain(root: Path, workflow: Workflow) -> GateChain:
    """Derive the highest contiguous valid gate chain without trusting state."""
    approved: list[tuple[str, dict[str, Any]]] = []
    states: dict[str, str] = {}
    issues: list[str] = []
    first_broken: str | None = None
    chain_open = True
    for phase in workflow.phases:
        path = gate_path(root, phase.id)
        if not path.is_file():
            states[phase.id] = "pending"
            if chain_open:
                chain_open = False
                first_broken = phase.id
            continue
        if not chain_open:
            states[phase.id] = "invalid"
            issues.append(
                f"Gate {phase.id} exists beyond the first unapproved or invalid phase {first_broken}"
            )
            continue
        try:
            validate_dependencies(root, workflow, phase)
            gate = validate_gate(root, workflow, phase.id)
        except CwError as exc:
            states[phase.id] = "invalid"
            chain_open = False
            first_broken = phase.id
            issues.append(f"{phase.id}: {exc.message}")
            continue
        states[phase.id] = "approved"
        approved.append((phase.id, gate))
    return GateChain(tuple(approved), states, tuple(issues), first_broken)


def valid_gate_prefix(root: Path, workflow: Workflow) -> list[tuple[str, dict[str, Any]]]:
    """Return the authoritative contiguous approval prefix.

    Every discovered gate is fully validated, including its configured
    dependencies.  A gate after a gap is corruption rather than progress.
    """
    chain = derive_gate_chain(root, workflow)
    if chain.issues:
        raise CwError(
            "Approval gates do not form an executable phase sequence",
            ErrorCode.INVALID_GATE,
            "Inspect gates and reopen the first affected phase.",
            details="\n".join(chain.issues),
        )
    return list(chain.approved)


def valid_gate_ids(root: Path, workflow: Workflow) -> list[str]:
    """Return fully verified contiguous gates in configured order."""
    return [phase_id for phase_id, _ in valid_gate_prefix(root, workflow)]


def derive_effective_workflow_state(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
) -> EffectiveWorkflowState:
    """Derive canonical workflow reality and compare it with persisted state."""
    chain = derive_gate_chain(root, workflow)
    approved = list(chain.approved)
    approved_count = len(approved)
    phase_count = len(workflow.phases)
    planned_scope_complete = approved_count == phase_count
    completion_mode = "contract" if workflow.completion_target is not None else "legacy"
    completion_satisfied = False
    if planned_scope_complete and workflow.completion_target is not None:
        try:
            from .completion import validate_completion_gate

            validate_completion_gate(root, workflow)
            completion_satisfied = True
        except CwError:
            completion_satisfied = False
    is_complete = planned_scope_complete and (
        workflow.completion_target is None or completion_satisfied
    )
    current = state.get("current_phase")
    latest_reference = (
        gate_path(root, approved[-1][0]).relative_to(root).as_posix()
        if approved else None
    )
    expected_current = None if planned_scope_complete else workflow.phases[approved_count].id
    if is_complete:
        expected_status = WorkflowState.COMPLETED
    elif planned_scope_complete:
        from .completion import derive_completion_status

        expected_status, expected_proposal = derive_completion_status(root, workflow, state)
    else:
        expected_status = WorkflowState.IN_PROGRESS
        expected_proposal = None
    latest_review = approved[-1][1].get("review_reference") if approved else None
    if not isinstance(latest_review, str):
        latest_review = approved[-1][1].get("review_file") if approved else None
    if not isinstance(latest_review, str):
        latest_review = None
    issues = list(chain.issues)
    if state.get("last_gate") != latest_reference:
        issues.append(f"last_gate must be {latest_reference or 'null'}")
    if planned_scope_complete:
        if current is not None:
            issues.append(
                "Planned-complete workflow still has an active phase"
                if workflow.completion_target is not None
                else "Completed workflow still has an active phase"
            )
        if workflow.completion_target is None and state.get("status") != WorkflowState.COMPLETED.value:
            issues.append("All configured legacy phases are approved, so state must be COMPLETED")
        elif workflow.completion_target is not None:
            if completion_satisfied and state.get("status") != WorkflowState.COMPLETED.value:
                issues.append("Valid completion evidence requires state COMPLETED")
            if not completion_satisfied and state.get("status") == WorkflowState.COMPLETED.value:
                issues.append("Contract-aware workflow is marked complete without valid completion evidence")
            if expected_status is WorkflowState.EXTENSION_PROPOSED and state.get("extension_proposal") != expected_proposal:
                issues.append(f"extension_proposal must be {expected_proposal}")
            if state.get("status") != expected_status.value:
                issues.append(f"completion state must be {expected_status.value}")
    else:
        if current != expected_current:
            issues.append(f"current_phase must be {expected_current}")
        if expected_current and chain.states.get(expected_current) == "approved":
            issues.append(f"current phase {expected_current} already has a valid gate")
        if state.get("status") == WorkflowState.COMPLETED.value:
            issues.append("workflow is marked complete without all approval gates")
    active_amendment_proposed = (
        state.get("status") == WorkflowState.PLAN_PROPOSED.value
        and isinstance(state.get("history"), list)
        and bool(state["history"])
        and isinstance(state["history"][-1], dict)
        and state["history"][-1].get("action") == "phase_artifacts_amended"
    )
    if approved and workflow.status == "PROPOSED" and not active_amendment_proposed:
        issues.append("an executing plan with approval gates cannot remain PROPOSED")
    if (
        expected_current is not None
        and state.get("status") == WorkflowState.IN_PROGRESS.value
        and state.get("last_review") == latest_review
        and state.get("attempt") != 0
        and not (
            state.get("superseded_plan_revisions")
            and state.get("revision_attempt", state.get("attempt")) == 0
        )
    ):
        issues.append("attempt must reset to 0 after phase advancement")
    if resolved_cw_metadata_error(state):
        issues.append("a resolved historical CW metadata error remains cached")

    readiness = readiness_path(root)
    if readiness.is_file():
        from .utils import load_json

        try:
            manifest = load_json(readiness)
            readiness_phase = manifest.get("phase") if isinstance(manifest, dict) else None
        except CwError:
            readiness_phase = None
        if readiness_phase != expected_current:
            issues.append(
                f"readiness belongs to {readiness_phase or 'an unknown phase'}, expected {expected_current or 'none'}"
            )
    return EffectiveWorkflowState(
        chain=chain,
        status=expected_status,
        current_phase=expected_current,
        approved_phases=tuple(phase_id for phase_id, _ in approved),
        approved_count=approved_count,
        remaining_count=max(0, phase_count - approved_count),
        active_count=0 if planned_scope_complete else 1,
        last_gate=latest_reference,
        last_review=latest_review,
        is_complete=is_complete,
        planned_scope_complete=planned_scope_complete,
        completion_mode=completion_mode,
        completion_satisfied=completion_satisfied,
        consistent=not issues,
        issues=tuple(issues),
    )


def derive_workflow_consistency(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
) -> EffectiveWorkflowState:
    """Compatibility name for the canonical effective-state derivation."""

    return derive_effective_workflow_state(root, workflow, state)


def validate_progress_state(root: Path, workflow: Workflow, state: dict[str, Any]) -> None:
    """Require cached operational state to agree with authoritative gates."""
    consistency = derive_workflow_consistency(root, workflow, state)
    if not consistency.consistent:
        raise CwError(
            "Workflow state is inconsistent with approval evidence",
            ErrorCode.STATE_INCONSISTENT,
            "Run: cw repair",
            details="\n".join(consistency.issues),
        )


def _review_attempt(root: Path, gate: dict[str, Any]) -> int | None:
    from .utils import load_json, safe_project_path

    reference = gate.get("review_reference") or gate.get("review_file")
    if not isinstance(reference, str):
        return None
    review = load_json(safe_project_path(root, reference, must_exist=True))
    attempt = review.get("attempt") if isinstance(review, dict) else None
    return attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else None


def reconstruct_approval_history(
    root: Path,
    state: dict[str, Any],
    approved: list[tuple[str, dict[str, Any]]],
) -> int:
    """Restore missing cached approval events from validated gate evidence.

    Gate/review files are authoritative. Existing semantic and infrastructure
    events are never removed or reordered, and evidence timestamps are reused
    verbatim rather than synthesized.
    """
    history = state.setdefault("history", [])
    added = 0
    for phase_id, gate in approved:
        reference = gate_path(root, phase_id).relative_to(root).as_posix()
        exists = any(
            isinstance(event, dict)
            and event.get("phase") == phase_id
            and event.get("action") in {"approved", "human_approved"}
            and event.get("gate") == reference
            for event in history
        )
        if exists:
            continue
        timestamp = gate.get("approved_at")
        attempt = _review_attempt(root, gate)
        if not isinstance(timestamp, str) or not timestamp or attempt is None:
            raise CwError(
                f"Approval history evidence is incomplete: {phase_id}",
                ErrorCode.INVALID_GATE,
            )
        approval = gate.get("approval")
        action = "human_approved" if isinstance(approval, dict) and approval.get("kind") == "human" else "approved"
        event: dict[str, Any] = {
            "timestamp": timestamp,
            "phase": phase_id,
            "action": action,
            "gate": reference,
        }
        if action == "approved":
            event["attempt"] = attempt
        history.append(event)
        added += 1
    return added


def reconstruct_revision_history(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
) -> int:
    """Restore missing REVISE events from fully validated semantic reviews."""
    from .utils import load_json

    history = state.setdefault("history", [])
    added = 0
    for path in sorted((root / ".cw" / "reviews").glob("*.json")):
        review = load_json(path)
        if (
            not isinstance(review, dict)
            or review.get("workflow") != workflow.id
            or review.get("kind") != "semantic_review"
            or review.get("decision") != "REVISE"
            or not isinstance(review.get("phase"), str)
        ):
            continue
        try:
            phase = workflow.phase(str(review["phase"]))
            decision, _, _, issues = validate_reviewer_result(phase, review, root=root)
        except (CwError, KeyError):
            continue
        attempt = review.get("attempt")
        timestamp = review.get("created_at")
        if (
            decision.value != "REVISE"
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
            or not isinstance(timestamp, str)
            or not timestamp
        ):
            continue
        exists = any(
            isinstance(event, dict)
            and event.get("phase") == phase.id
            and event.get("action") == "revision_required"
            and event.get("attempt") == attempt
            for event in history
        )
        if exists:
            continue
        history.append({
            "timestamp": timestamp,
            "phase": phase.id,
            "action": "revision_required",
            "attempt": attempt,
            "issues": issues,
        })
        added += 1
    return added


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
    effective = derive_effective_workflow_state(root, workflow, state)
    if effective.chain.issues:
        raise CwError(
            "Approval gates do not form an executable phase sequence",
            ErrorCode.INVALID_GATE,
            "Inspect gates and reopen the first affected phase.",
            details="\n".join(effective.chain.issues),
        )
    approved_evidence = list(effective.chain.approved)
    approved = list(effective.approved_phases)
    if not approved:
        return workflow, False

    changed = False
    if workflow.status == "PROPOSED":
        set_plan_status(root, "APPROVED")
        workflow = load_workflow(root)
        changed = True

    history_added = reconstruct_revision_history(root, workflow, state)
    history_added += reconstruct_approval_history(root, state, approved_evidence)
    changed = changed or history_added > 0

    current = state.get("current_phase")
    expected_current = effective.current_phase
    expected_status = effective.status.value
    latest_id, latest_gate = approved_evidence[-1]
    latest_reference = gate_path(root, latest_id).relative_to(root).as_posix()
    latest_review = latest_gate.get("review_reference") or latest_gate.get("review_file")
    advanced_attempt_stale = (
        expected_current is not None
        and state.get("status") == WorkflowState.IN_PROGRESS.value
        and state.get("last_review") == latest_review
        and state.get("attempt") != 0
        and not (
            state.get("superseded_plan_revisions")
            and state.get("revision_attempt", state.get("attempt")) == 0
        )
    )
    position_stale = (
        current != expected_current
        or state.get("last_gate") != latest_reference
        or (effective.planned_scope_complete and state.get("status") != expected_status)
        or advanced_attempt_stale
        or resolved_cw_metadata_error(state)
    )
    expected_proposal = None
    if effective.planned_scope_complete and workflow.completion_target is not None:
        from .completion import derive_completion_status

        _, expected_proposal = derive_completion_status(root, workflow, state)
        position_stale = position_stale or state.get("extension_proposal") != expected_proposal
    if position_stale:
        latest = workflow.phase(latest_id)
        state["last_gate"] = gate_path(root, latest.id).relative_to(root).as_posix()
        if isinstance(latest_review, str):
            state["last_review"] = latest_review
        state["last_error"] = None
        state["infrastructure_error"] = None
        if effective.planned_scope_complete:
            state["current_phase"] = None
            state["status"] = expected_status
            state["extension_proposal"] = expected_proposal
            if expected_status == WorkflowState.COMPLETED.value and workflow.completion_target is not None:
                from .completion import completion_gate_path
                from .utils import load_json

                completion_gate = completion_gate_path(root)
                gate_data = load_json(completion_gate)
                state["last_completion_gate"] = completion_gate.relative_to(root).as_posix()
                if isinstance(gate_data, dict) and isinstance(gate_data.get("cycle"), int):
                    state["completion_cycle"] = gate_data["cycle"]
            state["attempt"] = 0
            state["revision_attempt"] = 0
        else:
            state["current_phase"] = workflow.phases[len(approved)].id
            state["status"] = WorkflowState.IN_PROGRESS.value
            state["attempt"] = 0
            state["revision_attempt"] = 0
        readiness_path(root).unlink(missing_ok=True)
        finish_session(root)
        changed = True
    elif readiness_path(root).is_file():
        from .utils import load_json

        try:
            readiness = load_json(readiness_path(root))
            readiness_phase = readiness.get("phase") if isinstance(readiness, dict) else None
        except CwError:
            readiness_phase = None
        if readiness_phase != expected_current:
            readiness_path(root).unlink(missing_ok=True)
            finish_session(root)
            changed = True
    return workflow, changed
