from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from cw.adapters.codex import CodexAdapter
from cw.agents.reviewer import human_approve, run_review
from cw.checks.deterministic import validate_phase
from cw.cli.commands import lifecycle as lifecycle_commands
from cw.cli.commands import read as read_commands
from cw.core.config import apply_policy, load_policy
from cw.core.diagnostics import record_diagnostic, state_error
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import validate_dependencies, validate_gate
from cw.core.integrity import snapshot_protected_paths, verify_protected_paths
from cw.core.locking import operation_lock
from cw.core.layout import validate_project_layout
from cw.core.models import WorkflowState
from cw.core.project import load_project, repository_root
from cw.core.session import create_session, finish_session, load_session, readiness_path
from cw.core.state import load_state, save_state, transition, validate_state
from cw.core.utils import utc_now
from cw.core.workflow import load_workflow
from cw.ui.console import Console, HELP, emit_json, error_summary


MUTATING = {"init", "plan", "start", "review", "retry", "repair"}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit stable JSON")
    parser.add_argument("--verbose", action="store_true", help="Show diagnostic detail")
    parser.add_argument("--quiet", action="store_true", help="Suppress normal text output")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cw", add_help=False)
    _common(root)
    subs = root.add_subparsers(dest="command")
    for name in ("init", "start", "status", "validate", "retry", "config", "version", "help"):
        item = subs.add_parser(name, add_help=True)
        _common(item)
    plan = subs.add_parser("plan", add_help=True)
    _common(plan)
    plan.add_argument("action", nargs="?", choices=("show", "approve", "rebuild"))
    plan.add_argument("--goal")
    review = subs.add_parser("review", add_help=True)
    _common(review)
    review.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    review.add_argument("--human-approve", action="store_true", help="Approve a pending human gate")
    history = subs.add_parser("history", add_help=True)
    _common(history)
    history.add_argument("--phase")
    doctor = subs.add_parser("doctor", add_help=True)
    _common(doctor)
    doctor.add_argument("--reviewer", action="store_true", help="Include a live reviewer connectivity check")
    error = subs.add_parser("error", add_help=True)
    _common(error)
    error.add_argument("--raw", action="store_true")
    repair_parser = subs.add_parser("repair", add_help=True)
    _common(repair_parser)
    repair_parser.add_argument("--reopen", metavar="PHASE", help="Back up gates and explicitly reopen a phase")
    return root


def _root() -> Path:
    return repository_root(Path.cwd())


def _context(root: Path) -> tuple[Any, dict[str, Any], Any]:
    validate_project_layout(root)
    project = load_project(root)
    workflow = load_workflow(root)
    if workflow.id != project.project_id or workflow.repository != project.project_id:
        raise CwError("Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair", details=f"Workflow: {workflow.repository or workflow.id}\nRepository: {project.project_id}")
    workflow = apply_policy(workflow, load_policy(root, workflow=workflow))
    state = load_state(root)
    if workflow.phases:
        validate_state(root, state, workflow)
    return project, state, workflow


def _git_branch(root: Path) -> str:
    return read_commands.git_branch(root)


def _status_payload(root: Path) -> dict[str, Any]:
    return read_commands.status_payload(root, _context)


def _render_status(console: Console, data: dict[str, Any], verbose: bool = False) -> None:
    read_commands.render_status(console, data, verbose)


def command_init(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_init(args, console, root_resolver=_root)


def command_plan(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_plan(
        args, console, root_resolver=_root, context=_context,
    )


def _current(workflow: Any, state: dict[str, Any]) -> Any:
    phase_id = state.get("current_phase")
    if not phase_id:
        raise CwError("No current phase", ErrorCode.INVALID_STATE, "Run: cw plan")
    try:
        return workflow.phase(phase_id)
    except KeyError as exc:
        raise CwError("Current phase is not in the plan", ErrorCode.INVALID_STATE) from exc


def command_start(args: argparse.Namespace, console: Console) -> int:
    if args.json:
        raise CwError(
            "JSON mode is not supported for cw start", ErrorCode.USAGE_ERROR,
            "Run: cw start", exit_code=2,
        )
    root = _root()
    _, state, workflow = _context(root)
    phase = _current(workflow, state)
    with operation_lock(root, "start"):
        if not args.json and readiness_path(root).exists():
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
            raise CwError(f"Cannot start while workflow is {status.value}", ErrorCode.INVALID_STATE, "Run: cw status")
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
        result = CodexAdapter().run_implementer(
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
                "timestamp": utc_now(), "phase": phase.id, "action": "protected_path_violation",
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
        code = ErrorCode(code_value) if code_value in {item.value for item in ErrorCode} else ErrorCode.WORKFLOW_ERROR
        raise CwError("Phase review failed", code, "Run: cw retry", details=raw_error)
    if status is WorkflowState.IN_PROGRESS and not readiness_path(root).exists():
        failure = CwError(
            "Codex implementer stopped without readiness", ErrorCode.IMPLEMENTER_PROCESS_ERROR,
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


def command_status(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_status(
        args, console, root_resolver=_root, context=_context, record_error=_record_error,
    )


def command_validate(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    _, state, workflow = _context(root)
    phase = _current(workflow, state)
    result = validate_phase(root, workflow, phase)
    payload = {"phase": phase.id, "passed": result.passed, "checks": result.checks, "artifact_hashes": result.artifact_hashes, "errors": result.errors}
    if args.json:
        emit_json(payload)
    else:
        console.header("Validate")
        console.item("→", f"{phase.id} · {phase.name}")
        console.line()
        for check in result.checks:
            console.item("✓" if check.get("status") != "failed" and check.get("exit_code", 0) == 0 else "✕", check["name"])
        console.line()
        console.line("Validation passed." if result.passed else "Validation failed.")
    if not result.passed:
        _record_error(
            CwError(
                "Deterministic validation failed", ErrorCode.WORKFLOW_ERROR,
                "Run: cw validate", details="\n".join(result.errors),
            ),
            source="validate",
        )
    return 0 if result.passed else 1


def _review_output(console: Console, phase: Any, report: dict[str, Any], workflow: Any) -> None:
    decision = report["decision"]
    console.header("Review")
    console.item("→", f"{phase.id} · {phase.name}")
    console.line()
    if decision == "APPROVE" and not phase.requires_human_approval:
        console.item("✓", "APPROVED")
        console.field("Gate", f".cw/gates/{phase.id}.approved.json")
        index = workflow.index(phase.id)
        if index + 1 < len(workflow.phases):
            console.field("Next", f"{workflow.phases[index + 1].id} · {workflow.phases[index + 1].name}")
    elif decision == "REVISE":
        console.item("✕", "REVISION REQUIRED")
        console.line()
        for issue in report.get("blocking_issues", []):
            console.wrapped(issue)
    else:
        console.item("!", "HUMAN REVIEW REQUIRED")


def command_review(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    _, state, workflow = _context(root)
    phase = _current(workflow, state)
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
            gate = human_approve(root, workflow, phase, state)
            report = {"decision": "APPROVE", "gate": gate.relative_to(root).as_posix(), "human": True}
        else:
            report = run_review(root, workflow, phase, state)
    if args.hook:
        decision = report.get("decision")
        if decision == "REVISE":
            reason = "CW independent review requires revision. Run: cw history"
            print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
        else:
            reason = "CW phase review completed. Run: cw status"
            print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
        return 0
    if args.json:
        emit_json(report)
    else:
        _review_output(console, phase, report, workflow)
    return 3 if report.get("decision") == "HUMAN_REVIEW_REQUIRED" or phase.requires_human_approval and not args.human_approve else 1 if report.get("decision") == "REVISE" else 0


def command_retry(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    _, state, workflow = _context(root)
    if WorkflowState(state["status"]) is not WorkflowState.ERROR:
        raise CwError("There is no retryable infrastructure error", ErrorCode.INVALID_STATE)
    error = str(state.get("last_error") or "")
    readiness_exists = readiness_path(root).is_file()
    if "IMPLEMENTER_PROCESS_ERROR" in error and readiness_exists:
        args.hook = False
        args.human_approve = False
        return command_review(args, console)
    if "IMPLEMENTER_PROCESS_ERROR" in error or (
        "CODEX_NOT_FOUND" in error and state.get("current_phase") and not readiness_exists
    ):
        state["last_error"] = None
        transition(root, state, WorkflowState.IN_PROGRESS)
        return command_start(args, console)
    if any(code in error for code in ("PLANNER_NETWORK_ERROR", "PLANNER_PROCESS_ERROR", "PLAN_TIMEOUT", "CODEX_NOT_FOUND")) and not state.get("current_phase"):
        goal = state.get("pending_goal")
        state["last_error"] = None
        transition(root, state, WorkflowState.PLANNING)
        args.action = None
        args.goal = goal
        return command_plan(args, console)
    if not any(code in error for code in ("REVIEWER_NETWORK_ERROR", "REVIEW_TIMEOUT", "REVIEWER_PROCESS_ERROR", "SCHEMA_VALIDATION_ERROR", "CODEX_NOT_FOUND")):
        raise CwError("The last error is not safely retryable", ErrorCode.INVALID_STATE, "Run: cw error")
    args.hook = False
    args.human_approve = False
    return command_review(args, console)


def command_history(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_history(args, console, root_resolver=_root, context=_context)


def _doctor(root: Path | None, reviewer: bool) -> list[dict[str, Any]]:
    return read_commands.doctor_checks(root, reviewer, context=_context, current_resolver=_current)


def command_doctor(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_doctor(
        args, console, root_resolver=_root, checks_provider=_doctor,
    )


def command_error(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_error(args, console, root_resolver=_root)


def command_repair(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_repair(
        args, console, root_resolver=_root, context=_context,
    )


def command_config(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_config(args, console, root_resolver=_root)


def command_version(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_version(args, console)


COMMANDS = {
    "init": command_init, "plan": command_plan, "start": command_start, "status": command_status,
    "validate": command_validate, "review": command_review, "retry": command_retry,
    "history": command_history, "doctor": command_doctor, "error": command_error,
    "repair": command_repair, "config": command_config, "version": command_version,
}


def _record_error(exc: CwError, *, source: str | None = None, traceback_text: str | None = None) -> None:
    try:
        root = repository_root(Path.cwd())
        record_diagnostic(root, exc, source=source, traceback_text=traceback_text)
    except Exception:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv if argv is not None else sys.argv[1:])
    if not values:
        values = ["start"]
    elif values in (["-h"], ["--help"]):
        values = ["help"]
    args = parser().parse_args(values)
    command = args.command or "help"
    console = Console(no_color=args.no_color, quiet=args.quiet)
    if command == "help":
        if args.json:
            emit_json({"commands": list(COMMANDS) + ["help"]})
        elif not args.quiet:
            print(HELP, end="")
        return 0
    try:
        return COMMANDS[command](args, console)
    except CwError as exc:
        _record_error(exc, source=command)
        if getattr(args, "hook", False):
            reason = f"{exc.message}. {exc.hint or 'Run: cw error'}"
            print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
            return 0
        if args.json:
            emit_json({"error": {"code": exc.code.value, "message": exc.message, "hint": exc.hint, "details": exc.details}})
        elif not args.quiet:
            title, detail = error_summary(exc.code.value, exc.message)
            console.item("✕", title)
            console.wrapped(detail)
            if exc.details and (args.verbose or exc.code is ErrorCode.WORKFLOW_PROJECT_MISMATCH):
                console.line()
                for line in exc.details.splitlines():
                    console.wrapped(line)
            if exc.hint:
                console.run(exc.hint.removeprefix("Run: "))
        return exc.exit_code
    except Exception as exc:
        internal = CwError(
            "Unexpected internal failure", ErrorCode.INTERNAL_ERROR,
            "Run: cw error", details=f"{type(exc).__name__}: {exc}",
        )
        _record_error(internal, source=command, traceback_text=traceback.format_exc())
        if args.json:
            emit_json({"error": {"code": internal.code.value, "message": internal.message, "hint": internal.hint}})
        elif not args.quiet:
            title, detail = error_summary(internal.code.value, internal.message)
            console.item("✕", title)
            console.wrapped(detail)
            if args.verbose:
                console.line()
                console.wrapped(internal.details or "")
            console.run("cw error")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
