from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any, Callable

from cw.checks.deterministic import validate_phase
from cw.core.diagnostics import state_error
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import validate_dependencies, validate_gate
from cw.core.integrity import snapshot_protected_paths, verify_protected_paths
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.session import create_session, finish_session, load_session, readiness_path
from cw.core.state import load_state, transition
from cw.core.utils import utc_now
from cw.ui.console import Console, emit_json


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
    _, state, workflow = context(root)
    phase = current_resolver(workflow, state)
    with operation_lock(root, "start"):
        if readiness_path(root).exists():
            raise CwError("A readiness manifest already exists", ErrorCode.INVALID_STATE, "Run: cw review")
        status = WorkflowState(state["status"])
        if status is WorkflowState.APPROVED:
            validate_gate(root, workflow, phase.id)
            index = workflow.index(phase.id)
            if index == len(workflow.phases) - 1:
                transition(root, state, WorkflowState.COMPLETED)
                console.item("✓", "Workflow completed")
                return 0
            state["current_phase"] = workflow.phases[index + 1].id
            state["attempt"] = 0
            phase = workflow.phases[index + 1]
            transition(root, state, WorkflowState.IN_PROGRESS)
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
    console.header("Start")
    console.item("→", f"{phase.id} · {phase.name}")
    console.field("Sandbox", "workspace-write")
    failure: CwError | None = None
    result = 0
    try:
        result = adapter_factory().run_implementer(
            root,
            prompt,
            allow_network=workflow.allow_network,
            session_id=session["session_id"],
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
        transition(root, state, WorkflowState.ERROR, force_error=True)
        if not readiness_path(root).exists() or failure.code is ErrorCode.PROTECTED_PATH_MODIFIED:
            finish_session(root)
        if failure.code is ErrorCode.PROTECTED_PATH_MODIFIED:
            readiness_path(root).unlink(missing_ok=True)
        raise failure
    state = load_state(root)
    status = WorkflowState(state["status"])
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
        console.header("Validate")
        console.item("→", f"{phase.id} · {phase.name}")
        console.line()
        for check in result.checks:
            passed = check.get("status") != "failed" and check.get("exit_code", 0) == 0
            console.item("✓" if passed else "✕", check["name"])
        console.line()
        console.line("Validation passed." if result.passed else "Validation failed.")
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
    decision = report["decision"]
    console.header("Review")
    console.item("→", f"{phase.id} · {phase.name}")
    console.line()
    if decision == "APPROVE" and not phase.requires_human_approval:
        console.item("✓", "APPROVED")
        console.field("Gate", f".cw/gates/{phase.id}.approved.json")
        index = workflow.index(phase.id)
        if index + 1 < len(workflow.phases):
            following = workflow.phases[index + 1]
            console.field("Next", f"{following.id} · {following.name}")
    elif decision == "REVISE":
        console.item("✕", "REVISION REQUIRED")
        console.line()
        for issue in report.get("blocking_issues", []):
            console.wrapped(issue)
    else:
        console.item("!", "HUMAN REVIEW REQUIRED")


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
    with operation_lock(root, "review"):
        if args.human_approve:
            gate = human_approver(root, workflow, phase, state)
            report = {
                "decision": "APPROVE",
                "gate": gate.relative_to(root).as_posix(),
                "human": True,
            }
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
        render_review(console, phase, report, workflow)
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
    review_command: Command,
    start_command: Command,
    plan_command: Command,
) -> int:
    root = root_resolver()
    _, state, _ = context(root)
    if WorkflowState(state["status"]) is not WorkflowState.ERROR:
        raise CwError("There is no retryable infrastructure error", ErrorCode.INVALID_STATE)
    error = str(state.get("last_error") or "")
    readiness_exists = readiness_path(root).is_file()
    if "IMPLEMENTER_PROCESS_ERROR" in error and readiness_exists:
        args.hook = False
        args.human_approve = False
        return review_command(args, console)
    if "IMPLEMENTER_PROCESS_ERROR" in error or (
        "CODEX_NOT_FOUND" in error and state.get("current_phase") and not readiness_exists
    ):
        state["last_error"] = None
        transition(root, state, WorkflowState.IN_PROGRESS)
        return start_command(args, console)
    planner_errors = (
        "PLANNER_NETWORK_ERROR", "PLANNER_PROCESS_ERROR", "PLAN_TIMEOUT", "CODEX_NOT_FOUND",
    )
    if any(code in error for code in planner_errors) and not state.get("current_phase"):
        goal = state.get("pending_goal")
        state["last_error"] = None
        transition(root, state, WorkflowState.PLANNING)
        args.action = None
        args.goal = goal
        return plan_command(args, console)
    reviewer_errors = (
        "REVIEWER_NETWORK_ERROR", "REVIEW_TIMEOUT", "REVIEWER_PROCESS_ERROR",
        "SCHEMA_VALIDATION_ERROR", "CODEX_NOT_FOUND",
    )
    if not any(code in error for code in reviewer_errors):
        raise CwError("The last error is not safely retryable", ErrorCode.INVALID_STATE, "Run: cw error")
    args.hook = False
    args.human_approve = False
    return review_command(args, console)
