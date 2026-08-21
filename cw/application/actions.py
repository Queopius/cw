from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable

from cw.agents.reviewer import run_review
from cw.checks.deterministic import inspect_completed_work, validate_phase
from cw.core.completion import run_completion_review
from cw.core.gates import gate_path, validate_dependencies
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.recovery import (
    readiness_is_valid,
    regenerate_readiness,
    retryable_infrastructure_error,
)
from cw.core.session import create_session, readiness_path
from cw.core.layout import safe_directory
from cw.core.state import load_state, save_state, transition
from cw.core.revisions import artifact_revision_metadata, validation_attempts
from cw.core.utils import atomic_json_new, utc_now
from cw.execution.runs import load_active_run
from cw.execution.processes import ProcessInspector
from cw.execution.session import active_batch

from .context import load_project_context
from .models import ApplicationError, ApplicationErrorCode
from .projects import ResolvedProject


BackendFactory = Callable[[], Any]


def _phase(workflow: Any, state: dict[str, Any]) -> Any:
    phase_id = state.get("current_phase")
    if not isinstance(phase_id, str):
        raise ApplicationError(
            ApplicationErrorCode.PHASE_NOT_ACTIVE,
            "There is no current authorized phase",
        )
    try:
        return workflow.phase(phase_id)
    except KeyError as exc:
        raise ApplicationError(
            ApplicationErrorCode.STATE_INCONSISTENT,
            "The current phase is not in the authorized workflow",
        ) from exc


def _reject_completion_boundary(state: dict[str, Any]) -> None:
    status = WorkflowState(str(state["status"]))
    if status is WorkflowState.COMPLETED:
        raise ApplicationError(
            ApplicationErrorCode.PROJECT_COMPLETED,
            "The project is completed",
        )
    if status in {
        WorkflowState.PLANNED_COMPLETE,
        WorkflowState.COMPLETION_REVIEW,
        WorkflowState.EXTENSION_PROPOSED,
        WorkflowState.COMPLETION_BLOCKED,
    }:
        raise ApplicationError(
            ApplicationErrorCode.COMPLETION_EXTENSION_PENDING,
            "No phase action is authorized at the Completion Contract boundary",
        )


def start_current_phase(project: ResolvedProject) -> dict[str, Any]:
    with operation_lock(project.root, "application-phase-start"):
        if active_batch(project.root, own_pid=os.getpid()) is not None:
            raise ApplicationError(
                ApplicationErrorCode.OPERATION_IN_PROGRESS,
                "A CW batch operation is already active",
                retryable=True,
            )
        active_run = load_active_run(project.root)
        if active_run is not None:
            inspector = ProcessInspector()
            if (
                inspector.inspect(active_run.get("supervisor_pid")).alive
                or inspector.inspect(active_run.get("process_pid")).alive
            ):
                raise ApplicationError(
                    ApplicationErrorCode.OPERATION_IN_PROGRESS,
                    "A CW execution is already active",
                    retryable=True,
                )
            raise ApplicationError(
                ApplicationErrorCode.STATE_INCONSISTENT,
                "An interrupted CW execution must be reconciled before phase start",
            )
        _, state, workflow = load_project_context(project.root)
        if not workflow.phases:
            raise ApplicationError(
                ApplicationErrorCode.PHASE_NOT_STARTABLE,
                "The workflow has no authorized phases",
            )
        _reject_completion_boundary(state)
        phase = _phase(workflow, state)
        if gate_path(project.root, phase.id).exists():
            raise ApplicationError(
                ApplicationErrorCode.PHASE_NOT_STARTABLE,
                "The current phase already has a valid gate",
            )
        if readiness_path(project.root).exists():
            raise ApplicationError(
                ApplicationErrorCode.PHASE_NOT_STARTABLE,
                "The current phase is already ready for review",
            )
        status = WorkflowState(str(state["status"]))
        if status in {WorkflowState.READY, WorkflowState.REVISION_REQUIRED, WorkflowState.PAUSED}:
            transition(project.root, state, WorkflowState.IN_PROGRESS)
        elif status is not WorkflowState.IN_PROGRESS:
            raise ApplicationError(
                ApplicationErrorCode.PHASE_NOT_STARTABLE,
                f"A phase cannot start while the workflow is {status.value}",
            )
        validate_dependencies(project.root, workflow, phase)
        session = create_session(project.root, workflow, phase)
        return {
            "phase": phase.id,
            "phase_name": phase.name,
            "workflow_status": WorkflowState(load_state(project.root)["status"]).value,
            "session_id": session["session_id"],
            "next": "Implement only the current authorized phase, then create readiness evidence",
        }


def _validation_path(root: Path, phase_id: str, operation_id: str) -> Path:
    token = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
    directory = safe_directory(root / ".cw" / "validation", ".cw/validation", create=True)
    return directory / f"{phase_id}-{token}.json"


def validate_current_phase(project: ResolvedProject, operation_id: str) -> dict[str, Any]:
    with operation_lock(project.root, "application-validation"):
        _, state, workflow = load_project_context(project.root)
        _reject_completion_boundary(state)
        phase = _phase(workflow, state)
        validation = validate_phase(project.root, workflow, phase)
        global_attempt, revision_attempt = validation_attempts(project.root, workflow, state)
        evidence = {
            "schema_version": 1,
            "kind": "phase_validation",
            "workflow": workflow.id,
            "phase": phase.id,
            "operation_id": operation_id,
            "status": "PASSED" if validation.passed else "FAILED",
            "checks": validation.checks,
            "artifact_hashes": validation.artifact_hashes,
            "errors": validation.errors,
            "created_at": utc_now(),
            "validation_attempt": global_attempt + 1,
            "revision_validation_attempt": revision_attempt + 1,
            **artifact_revision_metadata(project.root, workflow, state, include_legacy=True),
        }
        path = _validation_path(project.root, phase.id, operation_id)
        try:
            atomic_json_new(path, evidence)
        except FileExistsError:
            # The operation store prevents a second execution. Treat an exact
            # durable validation record as a safe recovery replay.
            from cw.core.utils import load_json

            existing = load_json(path)
            if existing != evidence:
                raise ApplicationError(
                    ApplicationErrorCode.OPERATION_CONFLICT,
                    "Validation evidence already exists with different content",
                )
        return {
            "phase": phase.id,
            "validation_status": evidence["status"],
            "checks": validation.checks,
            "artifact_hashes": validation.artifact_hashes,
            "errors": validation.errors,
            "evidence": path.relative_to(project.root).as_posix(),
        }


def request_current_review(
    project: ResolvedProject,
    backend_factory: BackendFactory,
) -> dict[str, Any]:
    with operation_lock(project.root, "application-review"):
        _, state, workflow = load_project_context(project.root)
        _reject_completion_boundary(state)
        phase = _phase(workflow, state)
        report = run_review(
            project.root, workflow, phase, state, backend_factory(),
        )
        refreshed = load_state(project.root)
        completion = None
        if (
            refreshed.get("status") == WorkflowState.PLANNED_COMPLETE.value
            and workflow.completion_target is not None
        ):
            backend = backend_factory()
            if not hasattr(backend, "run_completion_reviewer"):
                raise ApplicationError(
                    ApplicationErrorCode.INFRASTRUCTURE_FAILURE,
                    "The configured reviewer cannot perform Completion Contract review",
                    retryable=True,
                )
            completion = run_completion_review(project.root, workflow, refreshed, backend)
        return {
            "phase": phase.id,
            "decision": report.get("decision"),
            "review": refreshed.get("last_review"),
            "gate": report.get("gate"),
            "next_phase": report.get("next_phase"),
            "planned_scope_complete": bool(report.get("planned_scope_complete")),
            "completion_review": completion,
        }


def retry_current_operation(
    project: ResolvedProject,
    backend_factory: BackendFactory,
) -> dict[str, Any]:
    with operation_lock(project.root, "application-retry"):
        _, state, workflow = load_project_context(project.root)
        _reject_completion_boundary(state)
        phase = _phase(workflow, state)
        if gate_path(project.root, phase.id).exists():
            raise ApplicationError(
                ApplicationErrorCode.RETRY_NOT_ALLOWED,
                "A phase with a valid gate cannot be retried",
            )
        metadata = retryable_infrastructure_error(state)
        if metadata is None or state.get("status") not in {
            WorkflowState.ERROR.value, WorkflowState.READY_FOR_REVIEW.value,
        }:
            raise ApplicationError(
                ApplicationErrorCode.RETRY_NOT_ALLOWED,
                "There is no retryable controlled operation",
            )
        if metadata.get("phase") not in {None, phase.id}:
            raise ApplicationError(
                ApplicationErrorCode.STATE_INCONSISTENT,
                "The retryable operation belongs to another phase",
            )
        operation = str(metadata.get("operation"))
        if operation == "codex":
            operation = "review" if readiness_path(project.root).exists() else "implementation"
        if operation not in {"review", "implementation"}:
            raise ApplicationError(
                ApplicationErrorCode.RETRY_NOT_ALLOWED,
                "This operation is outside the controlled MCP retry surface",
            )
        started_at = utc_now()
        state.setdefault("history", []).append({
            "timestamp": started_at,
            "phase": phase.id,
            "action": "retry_started",
            "operation": operation,
        })
        state["infrastructure_error"] = {**metadata, "retry_started_at": started_at}
        save_state(project.root, state)

        if operation == "review":
            if readiness_is_valid(project.root, workflow, phase):
                state["last_error"] = None
                if state["status"] == WorkflowState.ERROR.value:
                    transition(project.root, state, WorkflowState.READY_FOR_REVIEW)
                else:
                    save_state(project.root, state)
            else:
                validation = inspect_completed_work(project.root, workflow, phase)
                if not validation.passed:
                    raise ApplicationError(
                        ApplicationErrorCode.RETRY_NOT_ALLOWED,
                        "Implemented work is not ready for review recovery",
                    )
                transition(project.root, state, WorkflowState.IN_PROGRESS)
                regenerate_readiness(project.root, workflow, phase, validation)
                state.setdefault("history", []).append({
                    "timestamp": utc_now(),
                    "phase": phase.id,
                    "action": "readiness_resume_started",
                    "operation": "review",
                })
                state["last_error"] = None
                transition(project.root, state, WorkflowState.READY_FOR_REVIEW)
            report = run_review(project.root, workflow, phase, state, backend_factory())
            refreshed = load_state(project.root)
            return {
                "retried": "review",
                "phase": phase.id,
                "decision": report.get("decision"),
                "review": refreshed.get("last_review"),
                "gate": report.get("gate"),
                "next_phase": report.get("next_phase"),
            }

        if state["status"] != WorkflowState.ERROR.value:
            raise ApplicationError(
                ApplicationErrorCode.RETRY_NOT_ALLOWED,
                "Implementation retry state is invalid",
            )
        state["last_error"] = None
        state["infrastructure_error"] = None
        transition(project.root, state, WorkflowState.IN_PROGRESS)
        session = create_session(project.root, workflow, phase)
        return {
            "retried": "implementation",
            "phase": phase.id,
            "session_id": session["session_id"],
        }
