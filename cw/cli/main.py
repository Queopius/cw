from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from cw import __version__
from cw.adapters.codex import CodexAdapter
from cw.agents.reviewer import human_approve, run_review
from cw.checks.deterministic import validate_phase
from cw.core.config import apply_policy, load_config, load_policy
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_dependencies, validate_gate
from cw.core.initialize import backup_metadata, initialize, repair
from cw.core.integrity import snapshot_protected_paths, verify_protected_paths
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.project import load_project, repository_root
from cw.core.session import create_session, load_session, readiness_path
from cw.core.state import bind_plan, initial_state, load_state, save_state, transition, validate_state
from cw.core.utils import atomic_json, load_json, utc_now
from cw.core.workflow import load_workflow, set_plan_status, write_workflow, workflow_hash
from cw.planning.planner import Planner
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
    result = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=False)
    return result.stdout.strip() or "detached HEAD"


def _status_payload(root: Path) -> dict[str, Any]:
    project, state, workflow = _context(root)
    current = state.get("current_phase")
    index = workflow.index(current) if current and workflow.phases else None
    gates: dict[str, bool] = {}
    gate_error = None
    for phase in workflow.phases:
        exists = gate_path(root, phase.id).is_file()
        gates[phase.id] = exists
        if exists:
            try:
                validate_gate(root, workflow, phase.id)
            except CwError as exc:
                gates[phase.id] = False
                gate_error = str(exc)
    return {
        "schema_version": 1, "project": project.project_id, "repository_root": str(root),
        "branch": _git_branch(root), "workflow": "INITIALIZED" if not workflow.phases else "ACTIVE",
        "plan": workflow.status, "state": state["status"], "phase": current,
        "phase_index": index, "phase_count": len(workflow.phases), "attempt": state.get("attempt", 0),
        "max_attempts": workflow.max_review_attempts, "ready": (root / ".cw" / "runtime" / "READY_FOR_REVIEW.json").is_file(),
        "gate": gates.get(current, False) if current else False, "gates": gates, "gate_error": gate_error,
        "phases": [{"id": p.id, "name": p.name, "depends_on": list(p.depends_on)} for p in workflow.phases],
        "last_error": state.get("last_error"),
    }


def _render_status(console: Console, data: dict[str, Any], verbose: bool = False) -> None:
    console.header()
    console.field("Project", data["project"])
    console.field("Branch", data["branch"])
    console.field("Workflow", data["workflow"])
    console.field("State", data["state"])
    console.field("Plan", data["plan"])
    if data["phase"]:
        current = data["phases"][data["phase_index"]]
        console.line()
        console.field("Phase", f"{current['id']} · {current['name']}")
        console.field("Progress", f"{data['phase_index'] + 1} / {data['phase_count']} phases")
        console.field("Attempt", f"{data['attempt']} / {data['max_attempts']}")
        console.line()
        for idx, phase in enumerate(data["phases"]):
            marker = "✓" if data["gates"].get(phase["id"]) else "→" if idx == data["phase_index"] else "·"
            console.item(marker, f"{phase['id'].split('-', 1)[0]:>2}  {phase['name']}")
        console.line()
        console.field("Readiness", "READY" if data["ready"] else "NOT READY")
        console.field("Gate", "APPROVED" if data["gate"] else "PENDING")
    else:
        console.line()
        console.field("Workflow", "INITIALIZED")
        console.field("Plan", "NOT CREATED")
        console.run("cw plan")
    if data.get("gate_error"):
        console.line()
        console.item("✕", "Approval gate invalidated")
        console.run("cw error")
    if data["state"] == "ERROR" and data.get("last_error"):
        code = data["last_error"].split(":", 1)[0]
        title, detail = error_summary(code, data["last_error"])
        console.line()
        console.item("✕", title)
        console.wrapped(detail)
        console.run("cw error")
    if verbose:
        console.line()
        console.field("Root", data["repository_root"])
        console.field("State file", ".cw/state.json")


def command_init(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    with operation_lock(root, "init"):
        project, created = initialize(root)
    payload = {"project": project.project_id, "created": created, "static": ".codex/", "runtime": ".cw/", "plan": "NOT_CREATED"}
    if args.json:
        emit_json(payload)
    else:
        console.header("Initialize")
        console.item("✓", "Workflow initialized" if created else "Workflow already initialized")
        console.field("Project", project.project_id)
        console.field("Static", ".codex/")
        console.field("Runtime", ".cw/")
        console.field("Plan", "NOT CREATED" if load_workflow(root).status == "NOT_CREATED" else load_workflow(root).status)
        if load_workflow(root).status == "NOT_CREATED":
            console.run("cw plan")
    return 0


def command_plan(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    project, state, workflow = _context(root)
    action = args.action
    if action == "show":
        payload = {"workflow": workflow.id, "status": workflow.status, "goal": workflow.goal, "phases": [
            {"id": p.id, "name": p.name, "objective": p.objective, "depends_on": list(p.depends_on), "artifacts": list(p.artifacts),
             "required_commands": [c.command for c in p.required_commands], "acceptance_criteria": [c.__dict__ if hasattr(c, "__dict__") else {"id": c.id, "description": c.description, "severity": c.severity} for c in p.acceptance_criteria],
             "requires_human_approval": p.requires_human_approval} for p in workflow.phases]}
        if args.json:
            emit_json(payload)
        else:
            console.header("Plan")
            console.field("Status", workflow.status)
            console.field("Goal", workflow.goal or "NOT DEFINED")
            console.line()
            for phase in workflow.phases:
                console.item("·", f"{phase.id}  {phase.name}")
                console.wrapped(phase.objective, 4)
            if not workflow.phases:
                console.item("!", "No plan has been created")
                console.run('cw plan --goal "..."')
        return 0
    if action == "approve":
        if workflow.status != "PROPOSED" or WorkflowState(state["status"]) is not WorkflowState.PLAN_PROPOSED:
            raise CwError("No proposed plan is awaiting approval", ErrorCode.INVALID_STATE)
        with operation_lock(root, "plan-approve"):
            set_plan_status(root, "APPROVED")
            workflow = load_workflow(root)
            state["workflow_sha256"] = workflow_hash(root / ".codex" / "workflow" / "phases.yaml")
            transition(root, state, WorkflowState.READY)
        payload = {"status": "READY", "phases": len(workflow.phases)}
        if args.json:
            emit_json(payload)
        else:
            console.header("Plan")
            console.item("✓", "Plan approved")
            console.field("Phases", len(workflow.phases))
            console.run("cw")
        return 0
    with operation_lock(root, "plan"):
        if action == "rebuild":
            backup_metadata(root)
            state = initial_state(project.project_id)
            save_state(root, state)
        current = WorkflowState(state["status"])
        if current is WorkflowState.PLANNING:
            pass
        elif current is not WorkflowState.UNINITIALIZED:
            if current is WorkflowState.PLAN_PROPOSED:
                transition(root, state, WorkflowState.PLANNING)
            else:
                raise CwError("Existing workflow must be rebuilt explicitly", ErrorCode.INVALID_STATE, "Run: cw plan rebuild --goal \"...\"")
        else:
            transition(root, state, WorkflowState.PLANNING)
        state["pending_goal"] = args.goal
        save_state(root, state)
        planner = Planner(
            workflow.human_gate_categories,
            backend=CodexAdapter(),
            timeout=workflow.review_timeout,
        )
        try:
            payload = planner.propose_plan(root, project.project_id, args.goal)
        except CwError as exc:
            if exc.code in {
                ErrorCode.CODEX_NOT_FOUND, ErrorCode.PLAN_TIMEOUT,
                ErrorCode.PLANNER_NETWORK_ERROR, ErrorCode.PLANNER_PROCESS_ERROR,
            }:
                state["last_error"] = f"{exc.code.value}: {exc.message}\n{exc.details or ''}".rstrip()
                transition(root, state, WorkflowState.ERROR, force_error=True)
            raise
        write_workflow(root / ".codex" / "workflow" / "phases.yaml", payload)
        workflow = load_workflow(root)
        bind_plan(root, state, workflow)
        transition(root, state, WorkflowState.PLAN_PROPOSED)
    output = {"status": "PROPOSED", "goal": workflow.goal, "phases": len(workflow.phases)}
    if args.json:
        emit_json(output)
    else:
        console.header("Plan")
        console.item("✓", "Plan proposed")
        console.field("Goal", workflow.goal)
        console.field("Phases", len(workflow.phases))
        console.run("cw plan show\n    cw plan approve")
    return 0


def _current(workflow: Any, state: dict[str, Any]) -> Any:
    phase_id = state.get("current_phase")
    if not phase_id:
        raise CwError("No current phase", ErrorCode.INVALID_STATE, "Run: cw plan")
    try:
        return workflow.phase(phase_id)
    except KeyError as exc:
        raise CwError("Current phase is not in the plan", ErrorCode.INVALID_STATE) from exc


def command_start(args: argparse.Namespace, console: Console) -> int:
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
        session = create_session(root, workflow, phase) if not args.json else None
    prompt = f"""Work only on CW phase {phase.id}: {phase.name}.
Objective: {phase.objective}
Read AGENTS.md and .codex/workflow/phases.yaml. Do not change workflow state, criteria, reviews, or gates.
Active implementation session: {session['session_id'] if session else ''}
When complete, create .cw/runtime/READY_FOR_REVIEW.json matching the installed schema,
including this exact session_id, and stop normally.
"""
    if args.json:
        emit_json({"phase": phase.id, "state": "IN_PROGRESS", "sandbox": "workspace-write"})
        return 0
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
            session_id=session["session_id"] if session else None,
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
        state["last_error"] = f"{failure.code.value}: {failure.message}\n{failure.details or ''}".rstrip()
        transition(root, state, WorkflowState.ERROR, force_error=True)
        raise failure
    return result


def command_status(args: argparse.Namespace, console: Console) -> int:
    data = _status_payload(_root())
    if args.json:
        emit_json(data)
    else:
        _render_status(console, data, args.verbose)
    return 1 if data["state"] == "ERROR" or data.get("gate_error") else 0


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
    root = _root()
    _, state, workflow = _context(root)
    events = [event for event in state.get("history", []) if not args.phase or event.get("phase") == args.phase]
    payload = {"workflow": workflow.id, "events": events}
    if args.json:
        emit_json(payload)
    else:
        console.header("History")
        if not events:
            console.item("·", "No workflow events recorded")
        for event in events:
            marker = "✓" if "approved" in event["action"] else "→" if event["action"] == "revision_required" else "!"
            console.item(marker, f"{event['phase']}  {event['action'].replace('_', ' ')}")
            console.field("When", event["timestamp"], 8)
            if "attempt" in event:
                console.field("Attempt", event["attempt"], 8)
    return 0


def _doctor(root: Path | None, reviewer: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name in ("git", "python3", "codex"):
        path = shutil.which(name)
        checks.append({"section": "Environment", "name": name.capitalize(), "status": "pass" if path else "error", "detail": path or "not found"})
    if root is None:
        checks.append({"section": "Environment", "name": "Repository", "status": "error", "detail": "not in a Git repository"})
        return checks
    checks.append({"section": "Environment", "name": "Repository", "status": "pass", "detail": str(root)})
    try:
        project, state, workflow = _context(root)
        checks.extend([
            {"section": "Workflow", "name": "Project identity", "status": "pass", "detail": project.project_id},
            {"section": "Workflow", "name": "phases.yaml", "status": "pass", "detail": workflow.status},
            {"section": "Workflow", "name": "State", "status": "pass", "detail": state["status"]},
        ])
        writable = os.access(root / ".cw", os.W_OK)
        checks.append({"section": "Security", "name": ".cw writable", "status": "pass" if writable else "error", "detail": "required"})
        snapshot_protected_paths(root, workflow.protected_paths)
        checks.append({"section": "Security", "name": "Protected paths", "status": "pass", "detail": f"{len(workflow.protected_paths)} enforced"})
        phase = _current(workflow, state) if workflow.phases and state.get("current_phase") else None
        session = load_session(root, workflow, phase) if phase else None
        checks.append({
            "section": "Security", "name": "Implementer session",
            "status": "pass" if session else "neutral",
            "detail": f"active for {session['phase']}" if session else "none",
        })
        checks.append({"section": "Security", "name": ".codex writable", "status": "neutral", "detail": "not required at runtime"})
        checks.append({"section": "Security", "name": "Hook trust", "status": "neutral", "detail": "managed by Codex; run /hooks if prompted"})
        if reviewer:
            adapter = CodexAdapter()
            schema = Path(__file__).resolve().parents[1] / "schemas" / "phase-review.schema.json"
            try:
                adapter.smoke_test(root, schema)
                checks.append({"section": "Workflow", "name": "Reviewer connectivity", "status": "pass", "detail": "independent read-only request succeeded"})
            except CwError as exc:
                checks.append({"section": "Workflow", "name": "Reviewer connectivity", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    except CwError as exc:
        checks.append({"section": "Workflow", "name": "Workflow integrity", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    return checks


def command_doctor(args: argparse.Namespace, console: Console) -> int:
    try:
        root = _root()
    except CwError:
        root = None
    checks = _doctor(root, args.reviewer)
    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    passed = sum(item["status"] == "pass" for item in checks)
    payload = {"checks": checks, "result": {"passed": passed, "warnings": warnings, "errors": errors}}
    if args.json:
        emit_json(payload)
    else:
        console.header("Doctor")
        section = None
        for check in checks:
            if check["section"] != section:
                section = check["section"]
                console.line(section)
            marker = {"pass": "✓", "warning": "!", "error": "✕", "neutral": "·"}[check["status"]]
            console.item(marker, f"{check['name']}  {check['detail']}")
        console.line()
        console.line("Result")
        console.item("✓", f"{passed} checks passed")
        console.item("·", f"{warnings} warnings")
        console.item("✕", f"{errors} errors")
    return 1 if errors else 0


def command_error(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    _, state, _ = _context(root)
    value = state.get("last_error")
    payload = {"error": value}
    if args.json:
        emit_json(payload)
    else:
        console.header("Error")
        if value:
            console.line(value if args.raw else value)
        else:
            console.item("✓", "No stored workflow error")
    return 1 if value else 0


def command_repair(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    reopened = None
    with operation_lock(root, "repair"):
        if args.reopen:
            _, state, workflow = _context(root)
            try:
                target = workflow.phase(args.reopen)
            except KeyError as exc:
                raise CwError(f"Unknown phase: {args.reopen}", ErrorCode.USAGE_ERROR, exit_code=2) from exc
            backup = backup_metadata(root)
            affected = {target.id}
            changed = True
            while changed:
                changed = False
                for phase in workflow.phases:
                    if phase.id not in affected and any(dep in affected for dep in phase.depends_on):
                        affected.add(phase.id); changed = True
            for phase_id in affected:
                gate_path(root, phase_id).unlink(missing_ok=True)
            state.update({
                "current_phase": target.id, "status": WorkflowState.IN_PROGRESS.value,
                "attempt": 0, "last_review": None, "last_gate": None, "last_error": None,
            })
            state.setdefault("history", []).append({"timestamp": utc_now(), "phase": target.id, "action": "reopened", "backup": backup.relative_to(root).as_posix()})
            save_state(root, state)
            reopened = target.id
        else:
            backup = repair(root)
    payload = {"repaired": reopened is None, "reopened": reopened, "backup": backup.relative_to(root).as_posix()}
    if args.json:
        emit_json(payload)
    else:
        console.header("Repair")
        console.item("✓", f"Phase reopened: {reopened}" if reopened else "Workflow metadata repaired")
        console.field("Backup", payload["backup"])
    return 0


def command_config(args: argparse.Namespace, console: Console) -> int:
    root = _root()
    load_project(root)
    workflow = load_workflow(root)
    config = load_config(root, workflow=workflow)
    if args.json:
        emit_json(config)
    else:
        console.header("Configuration")
        for key, value in config.items():
            console.field(key, json.dumps(value) if isinstance(value, (list, dict)) else str(value).lower() if isinstance(value, bool) else value, 24)
        console.line()
        console.wrapped("Precedence: defaults < global (~/.config/cw/config.toml) < project (.cw/config.toml) < command-line flags")
    return 0


def command_version(args: argparse.Namespace, console: Console) -> int:
    payload = {"name": "CW", "brand": "CW by Queopius", "product": "Codex Workflow", "version": __version__}
    if args.json:
        emit_json(payload)
    else:
        console.line(f"CW by Queopius {__version__}")
        console.line("Codex Workflow")
    return 0


COMMANDS = {
    "init": command_init, "plan": command_plan, "start": command_start, "status": command_status,
    "validate": command_validate, "review": command_review, "retry": command_retry,
    "history": command_history, "doctor": command_doctor, "error": command_error,
    "repair": command_repair, "config": command_config, "version": command_version,
}


def _record_error(exc: CwError) -> None:
    try:
        root = repository_root(Path.cwd())
        log = root / ".cw" / "logs" / "errors.jsonl"
        if log.parent.is_dir():
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"timestamp": utc_now(), "code": exc.code.value, "message": exc.message, "details": exc.details}) + "\n")
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
        _record_error(exc)
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
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
