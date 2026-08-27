from __future__ import annotations

import argparse
import copy
import json
import os
import time
from collections.abc import Callable
from contextlib import nullcontext as _nullcontext
from pathlib import Path
from typing import Any

from cw.checks.deterministic import inspect_completed_work, validate_phase
from cw.core.diagnostics import state_error
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_dependencies, validate_gate
from cw.core.initialize import backup_metadata
from cw.core.integrity import snapshot_protected_paths, verify_protected_paths
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.progress import derive_effective_workflow_state
from cw.core.recovery import (
    mark_infrastructure_error,
    migrate_legacy_reviewer_error,
    readiness_is_valid,
    regenerate_readiness,
    retryable_infrastructure_error,
)
from cw.core.session import create_session, finish_session, load_session, readiness_path
from cw.core.state import advance_after_approval, load_state, save_state, transition
from cw.core.utils import utc_now
from cw.execution.context import execution_event_sink
from cw.execution.events import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionState,
    StartupProfile,
)
from cw.execution.processes import ProcessInspector
from cw.execution.runs import RunRecorder, load_active_run, new_run_id
from cw.execution.session import active_batch
from cw.integrations.config import project_requirements
from cw.integrations.manager import IntegrationManager
from cw.ui.console import Console, emit_json
from cw.ui.live import LiveExecutionObserver
from cw.ui.renderers import (
    render_completed_action,
    render_completed_start,
    render_review_result,
    render_review_start,
    render_start,
    render_transition,
    render_update_notice,
    render_validation,
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


def _require_review_hook_postcondition(
    root: Path,
    workflow: Any,
    phase: Any,
    report: dict[str, Any],
) -> None:
    """Refuse a successful hook response until its durable transition is proven."""

    state = load_state(root)
    readiness = readiness_path(root)
    readiness_present = readiness.exists() or readiness.is_symlink()
    decision = report.get("decision")
    valid = False
    if decision == "APPROVE" and not report.get("human"):
        try:
            validate_gate(root, workflow, phase.id)
        except CwError:
            valid = False
        else:
            status = state.get("status")
            current = state.get("current_phase")
            valid = not readiness_present and (
                status == WorkflowState.IN_PROGRESS.value
                and isinstance(current, str)
                and current != phase.id
                or status in {
                    WorkflowState.PLANNED_COMPLETE.value,
                    WorkflowState.COMPLETED.value,
                }
                and current is None
            )
    elif decision == "REVISE":
        valid = (
            not readiness_present
            and state.get("status") == WorkflowState.REVISION_REQUIRED.value
        )
    elif decision == "HUMAN_REVIEW_REQUIRED":
        valid = (
            not readiness_present
            and state.get("status") == WorkflowState.HUMAN_REVIEW_REQUIRED.value
        )
    if not valid:
        raise CwError(
            "Review hook durable postcondition failed",
            ErrorCode.INTEGRITY_ERROR,
            "Run: cw status",
        )


def command_start(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    current_resolver: CurrentResolver,
    adapter_factory: AdapterFactory,
) -> int:
    preflight_started = time.monotonic()
    root = root_resolver()
    if active_batch(root, own_pid=os.getpid()) is not None:
        raise CwError("Workflow batch is already running", ErrorCode.LOCKED, "Run: cw status")
    existing_run = load_active_run(root)
    if existing_run is not None:
        inspector = ProcessInspector()
        supervisor = inspector.inspect(existing_run.get("supervisor_pid"))
        child = inspector.inspect(existing_run.get("process_pid"))
        if supervisor.alive or child.alive:
            raise CwError(
                "Existing CW execution detected", ErrorCode.LOCKED,
                "Run: cw status",
                details=f"Run: {existing_run.get('run_id')}\nPhase: {existing_run.get('phase')}",
            )
        raise CwError(
            "Interrupted CW execution detected", ErrorCode.INVALID_STATE,
            "Run: cw repair",
            details=f"Run: {existing_run.get('run_id')}\nProgress was preserved.",
        )
    _, state, workflow = context(root)
    if not workflow.phases:
        raise CwError(
            "Development plan required",
            ErrorCode.PLAN_REQUIRED,
            "Run: cw plan",
            exit_code=3,
        )
    effective = derive_effective_workflow_state(root, workflow, state)
    if effective.is_complete:
        if args.json:
            emit_json({
                "event_type": "WORKFLOW_COMPLETED",
                "status": "COMPLETED",
                "approved": len(workflow.phases),
                "phases": len(workflow.phases),
                "implementation_started": False,
            })
        else:
            render_completed_start(console, workflow)
        return 0
    if effective.planned_scope_complete:
        raise CwError(
            "Planned scope is complete; no implementation phase is authorized",
            ErrorCode.INVALID_STATE,
            "Run: cw completion review",
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
        protected_before = snapshot_protected_paths(
            root, workflow.protected_paths, workflow=workflow, phase=phase,
        )
        run_id = new_run_id()
        session = create_session(root, workflow, phase, run_id=run_id)
        recorder = RunRecorder(
            root,
            run_id=run_id,
            phase_id=phase.id,
            role="implementation",
            session_id=session["session_id"],
        )
    prompt = f"""Work only on CW phase {phase.id}: {phase.name}.
Objective: {phase.objective}
Read AGENTS.md and .codex/workflow/phases.yaml. Do not change workflow state, criteria, reviews, or gates.
Active implementation session: {session['session_id']}
When complete, create .cw/runtime/READY_FOR_REVIEW.json matching the installed schema,
including this exact session_id, and stop normally.
"""
    approved = sum(gate_path(root, item.id).is_file() for item in workflow.phases)
    start_data = {
        "project": workflow.id,
        "number": phase.id.split("-", 1)[0],
        "name": phase.name,
        "approved": approved,
        "total": len(workflow.phases),
        "attempt": state.get("attempt", 0),
        "max_attempts": workflow.max_review_attempts,
        "run_id": run_id,
    }
    if args.json:
        emit_json({"event_type": "RUN_STARTED", "state": "PREFLIGHT", **start_data})
    else:
        render_start(console, start_data)
    observer = LiveExecutionObserver(
        console,
        recorder,
        role="implementation",
        json_mode=args.json,
        verbose=args.verbose,
    )
    profile = StartupProfile(preflight_ms=max(0, round((time.monotonic() - preflight_started) * 1000)))
    observer.set_profile(profile)
    notice = None if getattr(args, "_batch_mode", False) or args.json else automatic_update_notice()
    if notice is not None:
        render_update_notice(console, {
            "latest": str(notice.latest), "installed": str(notice.installed),
            "level": notice.level,
        })
    failure: CwError | None = None
    result = 0
    try:
        with execution_event_sink(observer):
            run_result = adapter_factory().run_implementer(
                root,
                prompt,
                allow_network=workflow.allow_network,
                session_id=session["session_id"],
                required_integrations=tuple(sorted(required_integrations)),
                timeout=int(getattr(args, "_batch_agent_timeout", 0)) or None,
            )
        result = int(getattr(run_result, "exit_code", run_result or 0))
        runtime_profile = getattr(run_result, "startup_profile", None)
        if isinstance(runtime_profile, dict):
            profile.spawn_ms = runtime_profile.get("spawn_ms")
            profile.session_init_ms = runtime_profile.get("session_init_ms")
            profile.first_event_ms = runtime_profile.get("first_event_ms")
            observer.set_profile(profile)
    except CwError as exc:
        failure = exc
    except KeyboardInterrupt:
        observer(ExecutionEvent(
            ExecutionEventType.STOP_REQUESTED,
            source_type="cw.interrupt",
            summary="Stop requested",
        ))
        failure = CwError(
            "Managed Codex execution was interrupted",
            ErrorCode.EXECUTION_INTERRUPTED,
            "Run: cw retry",
            details=f"Run {run_id} stopped by the user. Progress was preserved.",
            exit_code=130,
        )
    try:
        verify_protected_paths(root, workflow, phase, protected_before)
    except CwError as exc:
        failure = exc
    if failure is not None:
        observer.finish(
            success=False,
            status=ExecutionState.STOPPING if failure.code is ErrorCode.EXECUTION_INTERRUPTED else None,
        )
        last_activity = str(recorder.payload.get("last_activity") or "Codex working")
        elapsed = float(recorder.payload.get("elapsed_seconds") or observer.tracker.elapsed())
        diagnostic_context = (
            f"Run: {run_id}\nLast activity: {last_activity}\n"
            f"Elapsed: {elapsed:.1f}s\nProgress was preserved."
        )
        failure.details = (
            f"{diagnostic_context}\n\n{failure.details}"
            if failure.details else diagnostic_context
        )
        if not args.json and not console.quiet:
            console.line()
            console.field("Last activity", last_activity)
            console.field("Elapsed", f"{elapsed:.1f}s")
            console.wrapped("Progress preserved.", 2)
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
            ErrorCode.BATCH_TIME_EXHAUSTED, ErrorCode.EXECUTION_INTERRUPTED,
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
    if state.get("current_phase") != phase.id or status in {
        WorkflowState.COMPLETED, WorkflowState.PLANNED_COMPLETE,
    }:
        # A trusted Stop hook may have completed review and advanced the phase
        # while the implementer process was still active.
        from cw.core.gates import validate_gate

        validate_gate(root, workflow, phase.id)
        following = (
            None if status in {WorkflowState.COMPLETED, WorkflowState.PLANNED_COMPLETE}
            else workflow.phase(str(state["current_phase"]))
        )
        if args.json:
            emit_json({
                "event_type": "RUN_COMPLETED",
                "run_id": run_id,
                "phase": phase.id,
                "status": "COMPLETED",
                "next_phase": following.id if following else None,
            })
        else:
            render_transition(console, phase, following)
        observer.finish(success=True)
        if status is WorkflowState.PLANNED_COMPLETE and workflow.completion_target is not None:
            from cw.core.completion import run_completion_review

            with operation_lock(root, "completion-review"):
                completion_report = run_completion_review(
                    root, workflow, load_state(root), adapter_factory(),
                )
            if args.json:
                emit_json({
                    "event_type": "COMPLETION_REVIEW_COMPLETED",
                    "decision": completion_report["decision"],
                    "cycle": completion_report["cycle"],
                })
            else:
                console.line()
                console.section("Completion review")
                console.field("Decision", completion_report["decision"].replace("_", " "))
                console.wrapped(completion_report["summary"], 2)
                if completion_report["decision"] == "EXTENSION_REQUIRED":
                    console.wrapped("Human authorization is required before CW can continue.", 2)
            return 0 if completion_report["decision"] == "SATISFIED" else 3
        return result
    if status is WorkflowState.ERROR:
        raw_error = str(state.get("last_error") or "")
        code_value = raw_error.split(":", 1)[0]
        known_codes = {item.value for item in ErrorCode}
        code = ErrorCode(code_value) if code_value in known_codes else ErrorCode.WORKFLOW_ERROR
        observer.finish(success=False)
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
        observer.finish(success=False)
        raise failure
    if status is WorkflowState.IN_PROGRESS and readiness_path(root).exists():
        if args.json:
            emit_json({
                "event_type": "RUN_COMPLETED",
                "run_id": run_id,
                "phase": phase.id,
                "status": "READY_FOR_REVIEW",
                "next": "cw review",
            })
        else:
            console.item("!", "Phase is ready; automatic review did not run")
            console.run("cw review")
    observer.finish(success=True)
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
    completion_reviewer: Callable[[Path, Any, dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    root = root_resolver()
    if getattr(args, "action", None) == "authorize-retry":
        from cw.core.review_infrastructure_recovery import authorize_legacy_retry

        required = (
            args.phase,
            args.review_ref,
            args.expected_review_sha256,
            args.expected_workflow_sha256,
            args.expected_state_sha256,
            args.reason,
        )
        if (
            not all(required)
            or bool(args.dry_run) == bool(args.apply)
            or not args.acknowledge_unverifiable_legacy
        ):
            raise CwError(
                "Legacy retry authorization requires all CAS values, reason, acknowledgement, and exactly one of --dry-run or --apply",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
        with operation_lock(root, "review-authorize-retry") if args.apply else _nullcontext():
            report = authorize_legacy_retry(
                root,
                *required,
                args.acknowledge_unverifiable_legacy,
                apply=args.apply,
            )
        if args.json:
            emit_json(report)
        else:
            console.header("Legacy review retry authorization")
            console.field("Classification", report["classification"])
            console.field("Changed", str(report["changed"]).lower())
            console.action(report["next_action"], "Run a fresh deterministic verification")
        return 0
    if getattr(args, "action", None) == "recover-infrastructure":
        from cw.core.review_infrastructure_recovery import (
            apply_review_infrastructure_recovery,
            preview_review_infrastructure_recovery,
            recover_review_infrastructure_transaction,
        )

        required = (
            args.phase, args.review_ref, args.expected_review_sha256,
            args.expected_workflow_sha256, args.expected_state_sha256, args.reason,
        )
        if not all(required) or bool(args.dry_run) == bool(args.apply):
            raise CwError(
                "Review infrastructure recovery requires phase, review, three CAS values, reason, and exactly one of --dry-run or --apply",
                ErrorCode.USAGE_ERROR, exit_code=2,
            )
        if args.hook or args.human_approve:
            raise CwError("Review recovery cannot be combined with hook or human approval", ErrorCode.USAGE_ERROR, exit_code=2)
        if args.dry_run:
            report = preview_review_infrastructure_recovery(root, *required)
        else:
            with operation_lock(root, "review-recover-infrastructure"):
                recover_review_infrastructure_transaction(root)
                report = apply_review_infrastructure_recovery(root, *required)
        if args.json:
            emit_json(report)
        else:
            console.header("Review infrastructure recovery")
            console.field("Result", report["result"])
            console.field("Phase", report["phase"])
            console.field("Review", report["review_reference"])
            console.field("Classification", report["classification"])
            console.field("Changed", str(report["changed"]).lower())
            console.field("Attempts restored", report["attempts_restored"])
            if report.get("backup"):
                console.field("Backup", report["backup"])
            console.action("cw retry --json", "Retry review as a separate governed operation")
        return 0
    if getattr(args, "action", None) is not None:
        raise CwError("Unknown review operation", ErrorCode.USAGE_ERROR, exit_code=2)
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
    review_observer: LiveExecutionObserver | None = None
    if show_live:
        render_review_start(console, phase)
        review_run_id = new_run_id()
        review_observer = LiveExecutionObserver(
            console,
            RunRecorder(root, run_id=review_run_id, phase_id=phase.id, role="review"),
            role="review",
            verbose=args.verbose,
        )
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
            report["workflow_completed"] = load_state(root)["status"] == WorkflowState.COMPLETED.value
        else:
            try:
                with execution_event_sink(review_observer if review_observer is not None else None):
                    report = reviewer(root, workflow, phase, state)
            except BaseException:
                if review_observer is not None:
                    review_observer.finish(success=False)
                raise
    if review_observer is not None:
        review_observer.finish(success=True)
    retry_context = getattr(args, "retry_context", None)
    if isinstance(retry_context, dict):
        report = {
            **report,
            "result": "REVIEW_RETRIED",
            "changed": True,
            "mutation": "semantic-review+state" + ("+gate" if report.get("gate") else ""),
            "retryable": False,
            "classification": retry_context.get("error_code"),
            "retry_operation": retry_context.get("operation"),
            "review_reference": load_state(root).get("last_review"),
            "readiness_available": readiness_path(root).is_file(),
            "idempotent_replay": False,
            "next_action": "cw status" if report.get("decision") == "APPROVE" else "Revise the implementation",
        }
    if args.hook:
        _require_review_hook_postcondition(root, workflow, phase, report)
        if report.get("decision") == "REVISE":
            reason = "CW independent review requires revision. Run: cw history"
        else:
            reason = "CW phase review completed. Run: cw status"
        print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
        return 0
    refreshed = load_state(root)
    if (
        completion_reviewer is not None
        and refreshed.get("status") == WorkflowState.PLANNED_COMPLETE.value
        and workflow.completion_target is not None
    ):
        with operation_lock(root, "completion-review"):
            completion_report = completion_reviewer(root, workflow, refreshed)
        report["completion_review"] = completion_report
    if args.json:
        emit_json(report)
    else:
        render_review_result(console, phase, report, workflow, include_header=not show_live)
    requires_human = (
        report.get("decision") == "HUMAN_REVIEW_REQUIRED"
        or phase.requires_human_approval and not args.human_approve
    )
    completion_decision = (report.get("completion_review") or {}).get("decision")
    return 3 if requires_human or completion_decision in {"EXTENSION_REQUIRED", "BLOCKED"} else 1 if report.get("decision") == "REVISE" else 0


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
    completion_command: Command,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    from cw.core.review_infrastructure_recovery import pending_legacy_authorization

    legacy_auth = pending_legacy_authorization(root, state)
    metadata: dict[str, Any] | None
    if workflow.phases and derive_effective_workflow_state(root, workflow, state).is_complete:
        payload = {
            "status": "COMPLETED",
            "approved": len(workflow.phases),
            "phases": len(workflow.phases),
            "retry_required": False,
            "implementation_started": False,
        }
        if args.json:
            emit_json(payload)
        else:
            render_completed_action(
                console,
                workflow,
                title="Retry",
                detail="No retry is required. No implementation session was started.",
            )
        return 0
    if legacy_auth is not None:
        metadata = {
            "error_code": ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR.value,
            "retryable": True,
            "operation": "review",
            "phase": legacy_auth.get("phase_id"),
            "occurred_at": utc_now(),
            "legacy": True,
        }
        state["legacy_retry_authorization_id"] = legacy_auth.get("authorization_id")
        save_state(root, state)
    elif not isinstance(state.get("infrastructure_error"), dict):
        # A direct retry of prototype-era state performs the same safe,
        # backed-up normalization as `cw repair` before taking recovery action.
        with operation_lock(root, "retry-migration"):
            backup_metadata(root)
            migrated = migrate_legacy_reviewer_error(root, workflow, state)
            if migrated is not None:
                state["last_error"] = None
                save_state(root, state)
    metadata = metadata if legacy_auth is not None else retryable_infrastructure_error(state)
    status = WorkflowState(state["status"])
    if metadata is None or status not in {
        WorkflowState.ERROR,
        WorkflowState.READY_FOR_REVIEW,
        WorkflowState.COMPLETION_BLOCKED, WorkflowState.REVISION_REQUIRED,
    }:
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

    if operation in {"completion_review", "extension_planning"}:
        if status is not WorkflowState.COMPLETION_BLOCKED or phase_id is not None:
            raise CwError("Completion retry state is invalid", ErrorCode.INVALID_STATE)
        args.action = "review"
        return completion_command(args, console)

    if operation in {"review", "verification"}:
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
        args.retry_context = metadata
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
