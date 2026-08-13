from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from cw.adapters.codex import CodexAdapter
from cw.core.diagnostics import state_error
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_gate
from cw.core.initialize import backup_metadata, initialize, repair as repair_metadata
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.recovery import mark_infrastructure_error
from cw.core.state import bind_plan, initial_state, save_state, transition
from cw.core.utils import utc_now
from cw.core.workflow import load_workflow, set_plan_status, write_workflow, workflow_hash
from cw.planning.planner import Planner
from cw.ui.console import Console, emit_json


RootResolver = Callable[[], Path]
ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]


def command_init(args: argparse.Namespace, console: Console, *, root_resolver: RootResolver) -> int:
    root = root_resolver()
    with operation_lock(root, "init"):
        project, created = initialize(root)
    workflow = load_workflow(root)
    payload = {
        "project": project.project_id,
        "created": created,
        "static": ".codex/",
        "runtime": ".cw/",
        "plan": "NOT_CREATED",
    }
    if args.json:
        emit_json(payload)
    else:
        console.header("Initialize")
        console.item("✓", "Workflow initialized" if created else "Workflow already initialized")
        console.field("Project", project.project_id)
        console.field("Static", ".codex/")
        console.field("Runtime", ".cw/")
        console.field("Plan", "NOT CREATED" if workflow.status == "NOT_CREATED" else workflow.status)
        if workflow.status == "NOT_CREATED":
            console.run("cw plan")
    return 0


def _plan_payload(workflow: Any) -> dict[str, Any]:
    return {
        "workflow": workflow.id,
        "status": workflow.status,
        "goal": workflow.goal,
        "phases": [
            {
                "id": phase.id,
                "name": phase.name,
                "objective": phase.objective,
                "depends_on": list(phase.depends_on),
                "artifacts": list(phase.artifacts),
                "required_commands": [command.command for command in phase.required_commands],
                "acceptance_criteria": [
                    criterion.__dict__ if hasattr(criterion, "__dict__") else {
                        "id": criterion.id,
                        "description": criterion.description,
                        "severity": criterion.severity.value,
                    }
                    for criterion in phase.acceptance_criteria
                ],
                "requires_human_approval": phase.requires_human_approval,
            }
            for phase in workflow.phases
        ],
    }


def _show_plan(
    args: argparse.Namespace,
    console: Console,
    root: Path,
    state: dict[str, Any],
    workflow: Any,
) -> int:
    payload = _plan_payload(workflow)
    payload["current_phase"] = state.get("current_phase")
    for phase in payload["phases"]:
        phase["status"] = "current" if phase["id"] == state.get("current_phase") else "pending"
        if gate_path(root, phase["id"]).is_file():
            try:
                validate_gate(root, workflow, phase["id"])
                phase["status"] = "approved"
            except CwError:
                phase["status"] = "invalid"
    if args.json:
        emit_json(payload)
    else:
        console.header("Development Plan")
        console.field("Project", workflow.id)
        console.field("Status", workflow.status)
        console.field("Phases", len(workflow.phases))
        if args.verbose:
            console.field("Goal", workflow.goal or "NOT DEFINED")
        console.line()
        for phase in workflow.phases:
            rendered = next(item for item in payload["phases"] if item["id"] == phase.id)
            marker = {"approved": "✓", "current": "→", "invalid": "!", "pending": "·"}[rendered["status"]]
            console.phase(marker, phase.id.split("-", 1)[0], phase.name)
            if args.verbose:
                console.wrapped(phase.objective, 6)
                if phase.depends_on:
                    console.field("Depends", ", ".join(phase.depends_on), 12)
                console.field("Criteria", len(phase.acceptance_criteria), 12)
        if not workflow.phases:
            console.item("!", "No plan has been created")
            console.run('cw plan --goal "..."')
    return 0


def _approve_plan(
    args: argparse.Namespace,
    console: Console,
    root: Path,
    state: dict[str, Any],
    workflow: Any,
) -> int:
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


def command_plan(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
) -> int:
    root = root_resolver()
    project, state, workflow = context(root)
    if args.action == "show":
        return _show_plan(args, console, root, state, workflow)
    if args.action == "approve":
        return _approve_plan(args, console, root, state, workflow)
    with operation_lock(root, "plan"):
        if args.action == "rebuild":
            backup_metadata(root)
            state = initial_state(project.project_id)
            save_state(root, state)
        current = WorkflowState(state["status"])
        if current is WorkflowState.PLANNING:
            pass
        elif current not in {WorkflowState.UNINITIALIZED, WorkflowState.INITIALIZED}:
            if current is WorkflowState.PLAN_PROPOSED:
                transition(root, state, WorkflowState.PLANNING)
            else:
                raise CwError(
                    "Existing workflow must be rebuilt explicitly",
                    ErrorCode.INVALID_STATE,
                    'Run: cw plan rebuild --goal "..."',
                )
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
                ErrorCode.CODEX_NOT_FOUND,
                ErrorCode.PLAN_TIMEOUT,
                ErrorCode.PLANNER_NETWORK_ERROR,
                ErrorCode.PLANNER_TRANSPORT_ERROR,
                ErrorCode.PLANNER_PROCESS_ERROR,
            }:
                state["last_error"] = state_error(exc)
                mark_infrastructure_error(
                    state, exc, operation="planning", phase=None,
                )
                transition(root, state, WorkflowState.ERROR, force_error=True)
            else:
                state["pending_goal"] = None
                state["last_error"] = None
                state["infrastructure_error"] = None
                transition(root, state, WorkflowState.INITIALIZED)
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
        if args.verbose and planner.last_stderr.strip():
            console.line()
            console.section("Planner diagnostics")
            diagnostic = planner.last_stderr.strip()
            if len(diagnostic) > 3000:
                diagnostic = "… diagnostic truncated …\n" + diagnostic[-3000:]
            for line in diagnostic.splitlines():
                console.wrapped(line, 2)
    return 0


def command_repair(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
) -> int:
    root = root_resolver()
    reopened = None
    with operation_lock(root, "repair"):
        if args.reopen:
            _, state, workflow = context(root)
            try:
                target = workflow.phase(args.reopen)
            except KeyError as exc:
                raise CwError(
                    f"Unknown phase: {args.reopen}", ErrorCode.USAGE_ERROR, exit_code=2,
                ) from exc
            backup = backup_metadata(root)
            affected = {target.id}
            changed = True
            while changed:
                changed = False
                for phase in workflow.phases:
                    if phase.id not in affected and any(dep in affected for dep in phase.depends_on):
                        affected.add(phase.id)
                        changed = True
            for phase_id in affected:
                gate_path(root, phase_id).unlink(missing_ok=True)
            state.update({
                "current_phase": target.id,
                "status": WorkflowState.IN_PROGRESS.value,
                "attempt": 0,
                "last_review": None,
                "last_gate": None,
                "last_error": None,
                "infrastructure_error": None,
            })
            state.setdefault("history", []).append({
                "timestamp": utc_now(),
                "phase": target.id,
                "action": "reopened",
                "backup": backup.relative_to(root).as_posix(),
            })
            save_state(root, state)
            reopened = target.id
        else:
            backup = repair_metadata(root)
    payload = {
        "repaired": reopened is None,
        "reopened": reopened,
        "backup": backup.relative_to(root).as_posix(),
    }
    if args.json:
        emit_json(payload)
    else:
        console.header("Repair")
        console.item("✓", f"Phase reopened: {reopened}" if reopened else "Workflow metadata repaired")
        console.field("Backup", payload["backup"])
    return 0
