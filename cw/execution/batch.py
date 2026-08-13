from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from cw import __version__
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import validate_dependencies, validate_gate
from cw.core.models import WorkflowState
from cw.core.progress import derive_effective_workflow_state
from cw.core.state import load_state
from cw.core.utils import utc_now

from .budget import ExecutionBudget
from .session import archive_batch, new_batch, save_batch


class Clock(Protocol):
    def monotonic(self) -> float: ...


class MonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()


PhaseExecutor = Callable[[str, float], int]


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    status: str
    completed: int
    requested: int
    elapsed_seconds: int
    reason: str
    current_phase: str | None
    exit_code: int
    reviewer_runs: int = 0
    semantic_revisions: int = 0
    approvals: int = 0


class BatchRunner:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or MonotonicClock()

    def run(
        self,
        root: Path,
        workflow: Any,
        budget: ExecutionBudget,
        execute_phase: PhaseExecutor,
        *,
        session: dict[str, Any] | None = None,
    ) -> BatchOutcome:
        state = load_state(root)
        effective = derive_effective_workflow_state(root, workflow, state)
        if effective.is_complete:
            return BatchOutcome(
                "COMPLETED", 0, budget.max_phases, 0,
                "workflow_complete", None, 0,
            )
        if not workflow.phases or not state.get("current_phase"):
            raise CwError("An approved development plan is required", ErrorCode.PLAN_REQUIRED, "Run: cw plan")
        if session is None:
            session = new_batch(str(state["current_phase"]), budget.max_phases, budget.max_wall_time_seconds, __version__)
        elif session.get("cw_version") != __version__:
            raise CwError("Interrupted batch belongs to another CW version", ErrorCode.UPDATE_INCOMPATIBLE)
        session["pid"] = __import__("os").getpid()
        session["status"] = "RUNNING"
        save_batch(root, session)
        started = self.clock.monotonic()
        base_elapsed = float(session.get("elapsed_seconds", 0))
        try:
            while True:
                elapsed = base_elapsed + (self.clock.monotonic() - started)
                completed = int(session.get("completed_phases", 0))
                state = load_state(root)
                current_id = state.get("current_phase")
                if state["status"] == WorkflowState.COMPLETED.value:
                    _validate_created_gates(root, workflow, session)
                    return self._finish(root, session, "COMPLETED", elapsed, "workflow_complete", current_id, 0)
                if completed >= budget.max_phases:
                    _validate_created_gates(root, workflow, session)
                    return self._finish(root, session, "COMPLETED", elapsed, "phase_budget_reached", current_id, 0)
                if elapsed >= budget.max_wall_time_seconds:
                    return self._finish(root, session, "BUDGET_EXHAUSTED", elapsed, "time_budget_reached", current_id, 4)
                if state["status"] == WorkflowState.HUMAN_REVIEW_REQUIRED.value:
                    return self._finish(root, session, "HUMAN_REVIEW_REQUIRED", elapsed, "human_review_required", current_id, 3)
                if state["status"] == WorkflowState.ERROR.value:
                    return self._finish(root, session, "FAILED", elapsed, "workflow_error", current_id, 1)
                if not isinstance(current_id, str):
                    return self._finish(root, session, "FAILED", elapsed, "no_current_phase", None, 1)
                phase = workflow.phase(current_id)
                revisions = _revision_count(state, current_id)
                if revisions >= budget.max_semantic_revisions_per_phase:
                    return self._finish(root, session, "FAILED", elapsed, "semantic_revision_budget_reached", current_id, 1)
                agent_runs = int(session.get("agent_runs", 0))
                max_agent_runs = budget.max_agent_runs or budget.max_phases * (budget.max_semantic_revisions_per_phase + 1)
                if agent_runs >= max_agent_runs:
                    return self._finish(root, session, "FAILED", elapsed, "agent_run_budget_reached", current_id, 1)
                validate_dependencies(root, workflow, phase)
                phase_started = self.clock.monotonic()
                history_start = len(state.get("history", []))
                session.setdefault("events", []).append({"timestamp": utc_now(), "action": "batch_phase_started", "phase": current_id})
                session["agent_runs"] = agent_runs + 1
                save_batch(root, session)
                remaining = max(1.0, budget.max_wall_time_seconds + budget.hard_grace_seconds - elapsed)
                result = execute_phase(current_id, remaining)
                elapsed = base_elapsed + (self.clock.monotonic() - started)
                after = load_state(root)
                _account_review_events(session, after.get("history", [])[history_start:], current_id)
                advanced = after.get("current_phase") != current_id or after["status"] == WorkflowState.COMPLETED.value
                if advanced:
                    validate_gate(root, workflow, current_id)
                    session["completed_phases"] = int(session.get("completed_phases", 0)) + 1
                    session.setdefault("completed", []).append({
                        "phase": current_id,
                        "duration_seconds": max(0, int(self.clock.monotonic() - phase_started)),
                    })
                    session.setdefault("events", []).append({"timestamp": utc_now(), "action": "batch_phase_completed", "phase": current_id})
                    session["elapsed_seconds"] = int(elapsed)
                    save_batch(root, session)
                    continue
                if after["status"] == WorkflowState.HUMAN_REVIEW_REQUIRED.value:
                    return self._finish(root, session, "HUMAN_REVIEW_REQUIRED", elapsed, "human_review_required", current_id, 3)
                if after["status"] == WorkflowState.REVISION_REQUIRED.value:
                    session["elapsed_seconds"] = int(elapsed)
                    save_batch(root, session)
                    continue
                if after["status"] == WorkflowState.ERROR.value:
                    return self._finish(root, session, "FAILED", elapsed, "infrastructure_or_workflow_error", current_id, 1)
                reason = "no_verified_gate" if result == 0 else "phase_operation_failed"
                return self._finish(root, session, "FAILED", elapsed, reason, current_id, result or 1)
        except CwError:
            elapsed = base_elapsed + (self.clock.monotonic() - started)
            self._finish(root, session, "FAILED", elapsed, "workflow_safety_error", load_state(root).get("current_phase"), 1)
            raise
        except KeyboardInterrupt:
            elapsed = base_elapsed + (self.clock.monotonic() - started)
            return self._finish(
                root, session, "STOPPED", elapsed, "user_interrupted",
                load_state(root).get("current_phase"), 130,
            )

    def _finish(
        self, root: Path, session: dict[str, Any], status: str, elapsed: float,
        reason: str, current_phase: str | None, exit_code: int,
    ) -> BatchOutcome:
        session.update({
            "status": status, "elapsed_seconds": max(0, int(elapsed)),
            "stop_reason": reason, "finished_at": utc_now(), "pid": None,
        })
        session.setdefault("events", []).append({"timestamp": utc_now(), "action": f"batch_{status.lower()}", "reason": reason})
        save_batch(root, session)
        archive_batch(root, session)
        return BatchOutcome(
            status, int(session.get("completed_phases", 0)),
            int(session.get("requested_phases", 0)), max(0, int(elapsed)),
            reason, current_phase, exit_code,
            int(session.get("reviewer_runs", 0)),
            int(session.get("semantic_revisions", 0)),
            int(session.get("approvals", 0)),
        )


def _revision_count(state: dict[str, Any], phase_id: str) -> int:
    return sum(
        event.get("phase") == phase_id and event.get("action") == "revision_required"
        for event in state.get("history", []) if isinstance(event, dict)
    )


def _validate_created_gates(root: Path, workflow: Any, session: dict[str, Any]) -> None:
    for item in session.get("completed", []):
        phase_id = item.get("phase") if isinstance(item, dict) else None
        if isinstance(phase_id, str):
            validate_gate(root, workflow, phase_id)


def _account_review_events(session: dict[str, Any], events: list[Any], phase_id: str) -> None:
    actions = [
        item.get("action") for item in events
        if isinstance(item, dict) and item.get("phase") == phase_id
    ]
    approvals = actions.count("approved") + actions.count("human_approved")
    revisions = actions.count("revision_required")
    human = actions.count("human_review_required")
    infrastructure = sum(action in {"infrastructure_failure", "review_infrastructure_error"} for action in actions)
    session["approvals"] = int(session.get("approvals", 0)) + approvals
    session["semantic_revisions"] = int(session.get("semantic_revisions", 0)) + revisions
    session["infrastructure_failures"] = int(session.get("infrastructure_failures", 0)) + infrastructure
    session["reviewer_runs"] = int(session.get("reviewer_runs", 0)) + approvals + revisions + human
