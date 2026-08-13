from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any, Callable

from cw.checks.deterministic import inspect_completed_work, validate_phase
from cw.core.diagnostics import state_error
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_dependencies
from cw.core.integrity import snapshot_protected_paths, verify_protected_paths
from cw.core.initialize import backup_metadata
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.recovery import (
    mark_infrastructure_error,
    readiness_is_valid,
    regenerate_readiness,
    retryable_infrastructure_error,
    migrate_legacy_reviewer_error,
)
from cw.core.session import create_session, finish_session, load_session, readiness_path
from cw.core.state import advance_after_approval, load_state, save_state, transition
from cw.core.utils import utc_now
from cw.execution.session import active_batch
from cw.integrations.config import project_requirements
from cw.integrations.manager import IntegrationManager
from cw.ui.console import Console, emit_json
from cw.ui.renderers import (
    render_review_result,
    render_review_start,
    render_start,
    render_transition,
    render_validation,
    render_update_notice,
)
from cw.update.service import automatic_update_notice


RootResolver = Callable[[], Path]
ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]
CurrentResolver = Callable[[Any, dict[str, Any]], Any]
AdapterFactory = Callable[[], Any]
ErrorRecorder = Callable[..., None]
ReviewRunner = Callable[[Path, Any, Any, dict[str, Any]], dict[str, Any]]
HumanApprover = Callable[[Path, Any, Any, dict[str, Any]], Path]
Command = Callable[[argparse.Namespace, Console], int]


def current_phase(workflow: Any, state: dict[str, Any]) -> Any:
    phase_id = state.get("current_phase")
    if not phase_id:
        raise CwError("No current phase", ErrorCode.INVALID_STATE, "Run: cw plan")
    try:
        return workflow.phase(phase_id)
    except KeyError as exc:
        raise CwError("Current phase is not in the plan", ErrorCode.INVALID_STATE) from exc


def command_start(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    current_resolver: CurrentResolver,
    adapter_factory: AdapterFactory,
) -> int:
    if args.json:
        raise CwError(
            "JSON mode is not supported for cw start",
            ErrorCode.USAGE_ERROR,
            "Run: cw start",
            exit_code=2,
        )
    root = root_resolver()
    if active_batch(root, own_pid=os.getpid()) is not None:
        raise CwError("Workflow batch is already running", ErrorCode.LOCKED, "Run: cw status")
    _, state, workflow = context(root)
    if not workflow.phases:
        raise CwError(
            "Development plan required",
            ErrorCode.PLAN_REQUIRED,
            "Run: cw plan",
            exit_code=3,
        )
    phase = current_resolver(workflow, state)
    required_integrations = project_requirements(root) | set(phase.required_integrations)
    IntegrationManager().preflight(root, required_integrations)
    with operation_lock(root, "start"):
        if readiness_path(root).exists():
            raise CwError("A readiness manifest already exists", ErrorCode.INVALID_STATE, "Run: cw review")
        status = WorkflowState(state["status"])
        if status is WorkflowState.APPROVED:
            # Compatibility for v0.1.3 projects that deferred advancement until
            # the next start. New approvals advance inside the domain operation.
            next_phase = advance_after_approval(
                root,
                state,
                workflow,
                phase,
                f".cw/gates/{phase.id}.approved.json",
                attempt=max(1, int(state.get("attempt", 0))),
                record_event=False,
            )
            if next_phase is None:
                console.item("✓", "Workflow completed")
                return 0
            phase = next_phase
        elif status in {WorkflowState.READY, WorkflowState.REVISION_REQUIRED, WorkflowState.PAUSED}:
            transition(root, state, WorkflowState.IN_PROGRESS)
        elif status is not WorkflowState.IN_PROGRESS:
            raise CwError(
                f"Cannot start while workflow is {status.value}",
                ErrorCode.INVALID_STATE,
                "Run: cw status",
            )
        validate_dependencies(root, workflow, phase)
        protected_before = snapshot_protected_paths(root, workflow.protected_paths)
        session = create_session(root, workflow, phase)
    prompt = f"""Work only on CW phase {phase.id}: {phase.name}.
Objective: {phase.objective}
Read AGENTS.md and .codex/workflow/phases.yaml. Do not change workflow state, criteria, reviews, or gates.
Active implementation session: {session['session_id']}
When complete, create .cw/runtime/READY_FOR_REVIEW.json matching the installed schema,
including this exact session_id, and stop normally.
"""
    approved = sum(gate_path(root, item.id).is_file() for item in workflow.phases)
    render_start(console, {
        "project": workflow.id,
        "number": phase.id.split("-", 1)[0],
        "name": phase.name,
        "approved": approved,
        "total": len(workflow.phases),
        "attempt": state.get("attempt", 0),
        "max_attempts": workflow.max_review_attempts,
    })
    notice = None if getattr(args, "_batch_mode", False) else automatic_update_notice()
    if notice is not None:
        render_update_notice(console, {
            "latest": str(notice.latest), "installed": str(notice.installed),
            "level": notice.level,
        })
    failure: CwError | None = None
    result = 0
    try:
        result = adapter_factory().run_implementer(
            root,
            prompt,
            allow_network=workflow.allow_network,
            session_id=session["session_id"],
            required_integrations=tuple(sorted(required_integrations)),
            timeout=int(getattr(args, "_batch_agent_timeout", 0)) or None,
        )
    except CwError as exc:
        failure = exc
    try:
        verify_protected_paths(root, workflow, phase, protected_before)
    except CwError as exc:
        failure = exc
    if failure is not None:
        if failure.code is ErrorCode.PROTECTED_PATH_MODIFIED and protected_before.state is not None:
            state = copy.deepcopy(protected_before.state)
            state.setdefault("history", []).append({
                "timestamp": utc_now(),
                "phase": phase.id,
                "action": "protected_path_violation",
            })
        else:
            state = load_state(root)
        state["last_error"] = state_error(failure)
        if failure.code in {
            ErrorCode.IMPLEMENTER_PROCESS_ERROR, ErrorCode.CODEX_NOT_FOUND,
            ErrorCode.BATCH_TIME_EXHAUSTED,
        }:
            mark_infrastructure_error(
                state, failure, operation="implementation", phase=phase.id,
            )
        transition(root, state, WorkflowState.ERROR, force_error=True)
        if not readiness_path(root).exists() or failure.code is ErrorCode.PROTECTED_PATH_MODIFIED:
            finish_session(root)
        if failure.code is ErrorCode.PROTECTED_PATH_MODIFIED:
            readiness_path(root).unlink(missing_ok=True)
        raise failure
    state = load_state(root)
    status = WorkflowState(state["status"])
    if state.get("current_phase") != phase.id or status is WorkflowState.COMPLETED:
        # A trusted Stop hook may have completed review and advanced the phase
        # while the implementer process was still active.
        from cw.core.gates import validate_gate

        validate_gate(root, workflow, phase.id)
        following = None if status is WorkflowState.COMPLETED else workflow.phase(str(state["current_phase"]))
        render_transition(console, phase, following)
        return result
    if status is WorkflowState.ERROR:
        raw_error = str(state.get("last_error") or "")
        code_value = raw_error.split(":", 1)[0]
        known_codes = {item.value for item in ErrorCode}
        code = ErrorCode(code_value) if code_value in known_codes else ErrorCode.WORKFLOW_ERROR
        raise CwError("Phase review failed", code, "Run: cw retry", details=raw_error)
    if status is WorkflowState.IN_PROGRESS and not readiness_path(root).exists():
        failure = CwError(
            "Codex implementer stopped without readiness",
            ErrorCode.IMPLEMENTER_PROCESS_ERROR,
            "Run: cw retry",
        )
        state["last_error"] = state_error(failure)
        mark_infrastructure_error(
            state, failure, operation="implementation", phase=phase.id,
        )
        transition(root, state, WorkflowState.ERROR, force_error=True)
        finish_session(root)
        raise failure
    if status is WorkflowState.IN_PROGRESS and readiness_path(root).exists():
        console.item("!", "Phase is ready; automatic review did not run")
        console.run("cw review")
    return result


def command_validate(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    current_resolver: CurrentResolver,
    record_error: ErrorRecorder,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    if not workflow.phases:
        raise CwError(
            "Nothing to validate",
            ErrorCode.NOTHING_TO_VALIDATE,
            "Run: cw plan",
            exit_code=3,
        )
    phase = current_resolver(workflow, state)
    result = validate_phase(root, workflow, phase)
    payload = {
        "phase": phase.id,
        "passed": result.passed,
        "checks": result.checks,
        "artifact_hashes": result.artifact_hashes,
        "errors": result.errors,
    }
    if args.json:
        emit_json(payload)
    else:
        render_validation(console, phase, result, verbose=args.verbose)
    if not result.passed:
        record_error(
            CwError(
                "Deterministic validation failed",
                ErrorCode.WORKFLOW_ERROR,
                "Run: cw validate",
                details="\n".join(result.errors),
            ),
            source="validate",
        )
    return 0 if result.passed else 1


def render_review(console: Console, phase: Any, report: dict[str, Any], workflow: Any) -> None:
    """Compatibility seam retained for callers and tests."""
    render_review_result(console, phase, report, workflow)


def command_review(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    current_resolver: CurrentResolver,
    reviewer: ReviewRunner,
    human_approver: HumanApprover,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    phase = current_resolver(workflow, state)
    ready = readiness_path(root)
    if args.hook and not ready.exists():
        print("{}")
        return 0
    if args.hook:
        session = load_session(root, workflow, phase)
        if (
            session is None
            or os.environ.get("CW_IMPLEMENTER_ACTIVE") != "1"
            or os.environ.get("CW_IMPLEMENTER_SESSION") != session["session_id"]
        ):
            print("{}")
            return 0
    show_live = not args.hook and not args.json and not args.human_approve
    if show_live:
        render_review_start(console, phase)
    with operation_lock(root, "review"):
        if args.human_approve:
            gate = human_approver(root, workflow, phase, state)
            report = {
                "decision": "APPROVE",
                "gate": gate.relative_to(root).as_posix(),
                "human": True,
            }
            index = workflow.index(phase.id)
            report["next_phase"] = workflow.phases[index + 1].id if index + 1 < len(workflow.phases) else None
            report["workflow_completed"] = index + 1 == len(workflow.phases)
        else:
            report = reviewer(root, workflow, phase, state)
    if args.hook:
        if report.get("decision") == "REVISE":
            reason = "CW independent review requires revision. Run: cw history"
        else:
            reason = "CW phase review completed. Run: cw status"
        print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
        return 0
    if args.json:
        emit_json(report)
    else:
        render_review_result(console, phase, report, workflow, include_header=not show_live)
    requires_human = (
        report.get("decision") == "HUMAN_REVIEW_REQUIRED"
        or phase.requires_human_approval and not args.human_approve
    )
    return 3 if requires_human else 1 if report.get("decision") == "REVISE" else 0


def command_retry(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    current_resolver: CurrentResolver,
    review_command: Command,
    start_command: Command,
    plan_command: Command,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    if not isinstance(state.get("infrastructure_error"), dict):
        # A direct retry of prototype-era state performs the same safe,
        # backed-up normalization as `cw repair` before taking recovery action.
        with operation_lock(root, "retry-migration"):
            backup_metadata(root)
            migrated = migrate_legacy_reviewer_error(root, workflow, state)
            if migrated is not None:
                state["last_error"] = None
                save_state(root, state)
    metadata = retryable_infrastructure_error(state)
    status = WorkflowState(state["status"])
    if metadata is None or status not in {WorkflowState.ERROR, WorkflowState.READY_FOR_REVIEW}:
        raise CwError("There is no retryable infrastructure error", ErrorCode.INVALID_STATE)
    phase_id = state.get("current_phase")
    if metadata.get("phase") not in {None, phase_id}:
        raise CwError("Infrastructure error belongs to another phase", ErrorCode.INVALID_STATE, "Run: cw repair")
    operation = str(metadata["operation"])
    if operation == "codex":
        operation = "planning" if phase_id is None else "review" if readiness_path(root).exists() else "implementation"
    elif operation == "implementation" and phase_id is not None:
        phase = current_resolver(workflow, state)
        if readiness_is_valid(root, workflow, phase):
            # An implementer may exit after successfully producing readiness.
            # Recovery should continue from that durable boundary, not rerun it.
            operation = "review"
    started_at = utc_now()
    state.setdefault("history", []).append({
        "timestamp": started_at,
        "phase": phase_id,
        "action": "retry_started",
        "operation": operation,
    })
    metadata = {**metadata, "retry_started_at": started_at}
    state["infrastructure_error"] = metadata
    save_state(root, state)

    if operation == "review":
        phase = current_resolver(workflow, state)
        with operation_lock(root, "retry-review"):
            if readiness_is_valid(root, workflow, phase):
                state["last_error"] = None
                if status is WorkflowState.ERROR:
                    transition(root, state, WorkflowState.READY_FOR_REVIEW)
                else:
                    save_state(root, state)
            else:
                validation = inspect_completed_work(root, workflow, phase)
                if not validation.passed:
                    raise CwError(
                        "Implemented work is not ready for readiness recovery",
                        ErrorCode.INVALID_STATE,
                        "Restore the required artifacts or checks, then run: cw retry",
                        details="\n".join(validation.errors),
                    )
                if status is WorkflowState.ERROR:
                    transition(root, state, WorkflowState.IN_PROGRESS)
                else:
                    transition(root, state, WorkflowState.IN_PROGRESS)
                regenerate_readiness(root, workflow, phase, validation)
                state.setdefault("history", []).append({
                    "timestamp": utc_now(),
                    "phase": phase.id,
                    "action": "readiness_resume_started",
                    "operation": "review",
                })
                state["last_error"] = None
                transition(root, state, WorkflowState.READY_FOR_REVIEW)
        args.hook = False
        args.human_approve = False
        return review_command(args, console)
    if operation == "implementation":
        if status is not WorkflowState.ERROR:
            raise CwError("Implementation retry state is invalid", ErrorCode.INVALID_STATE)
        state["last_error"] = None
        state["infrastructure_error"] = None
        transition(root, state, WorkflowState.IN_PROGRESS)
        return start_command(args, console)
    if operation == "planning" and not phase_id:
        goal = state.get("pending_goal")
        state["last_error"] = None
        state["infrastructure_error"] = None
        transition(root, state, WorkflowState.PLANNING)
        args.action = None
        args.goal = goal
        return plan_command(args, console)
    raise CwError("The infrastructure error cannot be retried safely", ErrorCode.INVALID_STATE, "Run: cw error")
