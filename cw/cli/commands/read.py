from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from cw import __version__
from cw.adapters.codex import CodexAdapter
from cw.checks.deterministic import load_readiness
from cw.core.audit import audit_history
from cw.core.diagnostics import legacy_diagnostic, load_diagnostic, raw_diagnostic
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_gate
from cw.core.history import history_timeline
from cw.core.integrity import snapshot_protected_paths
from cw.core.schema import SCHEMA_VERSION
from cw.core.session import load_session, process_is_alive, readiness_path
from cw.core.utils import load_json
from cw.ui.console import Console, emit_json, error_summary
from cw.ui.renderers import render_doctor, render_history, render_status as render_status_view


RootResolver = Callable[[], Path]
ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]
CurrentResolver = Callable[[Any, dict[str, Any]], Any]
ErrorRecorder = Callable[..., None]
DoctorProvider = Callable[[Path | None, bool], list[dict[str, Any]]]


def git_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=root,
        text=True, capture_output=True, check=False,
    )
    return result.stdout.strip() or "detached HEAD"


def status_payload(root: Path, context: ContextLoader) -> dict[str, Any]:
    project, state, workflow = context(root)
    current = state.get("current_phase")
    index = workflow.index(current) if current and workflow.phases else None
    gates: dict[str, bool] = {}
    gate_states: dict[str, str] = {}
    invalid_gates: list[str] = []
    gate_error = None
    gate_error_code = None
    gate_error_details = None
    for phase in workflow.phases:
        exists = gate_path(root, phase.id).is_file()
        gates[phase.id] = exists
        gate_states[phase.id] = "pending"
        if exists:
            try:
                validate_gate(root, workflow, phase.id)
                gate_states[phase.id] = "approved"
            except CwError as exc:
                gates[phase.id] = False
                gate_states[phase.id] = "invalid"
                invalid_gates.append(phase.id)
                gate_error = str(exc)
                gate_error_code = exc.code.value
                gate_error_details = exc.details
    return {
        "schema_version": SCHEMA_VERSION, "project": project.project_id,
        "repository_root": str(root), "branch": git_branch(root),
        "workflow": "INITIALIZED" if not workflow.phases else "ACTIVE",
        "plan": workflow.status, "state": state["status"], "phase": current,
        "phase_index": index, "position": index + 1 if index is not None else None,
        "phase_count": len(workflow.phases), "approved_count": sum(gates.values()),
        "attempt": state.get("attempt", 0), "max_attempts": workflow.max_review_attempts,
        "ready": (root / ".cw" / "runtime" / "READY_FOR_REVIEW.json").is_file(),
        "gate": gates.get(current, False) if current else False, "gates": gates,
        "gate_states": gate_states, "invalid_gates": invalid_gates,
        "gate_error": gate_error, "gate_error_code": gate_error_code,
        "gate_error_details": gate_error_details,
        "phases": [
            {
                "id": phase.id,
                "number": phase.id.split("-", 1)[0],
                "name": phase.name,
                "depends_on": list(phase.depends_on),
            }
            for phase in workflow.phases
        ],
        "last_error": state.get("last_error"),
        "infrastructure_error": state.get("infrastructure_error"),
    }


def render_status(console: Console, data: dict[str, Any], verbose: bool = False) -> None:
    render_status_view(console, data, verbose=verbose)


def command_status(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    record_error: ErrorRecorder,
) -> int:
    root = root_resolver()
    data = status_payload(root, context)
    if data.get("gate_error"):
        code = ErrorCode(data.get("gate_error_code") or ErrorCode.INVALID_GATE.value)
        record_error(
            CwError(data["gate_error"], code, "Run: cw repair --reopen <phase>", data.get("gate_error_details")),
            source="status",
        )
    if args.json:
        emit_json(data)
    else:
        render_status(console, data, args.verbose)
    return 1 if data["state"] == "ERROR" or data.get("gate_error") else 0


def command_history(
    args: argparse.Namespace, console: Console, *, root_resolver: RootResolver, context: ContextLoader,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    audit_history(root, workflow, state)
    phases = history_timeline(root, workflow, state)
    if args.phase:
        phases = [
            phase for phase in phases
            if phase["phase"] == args.phase or phase["number"] == args.phase
        ]
    events = [event for event in state.get("history", []) if not args.phase or event.get("phase") == args.phase]
    payload = {"workflow": workflow.id, "phases": phases, "events": events}
    if args.json:
        emit_json(payload)
    else:
        render_history(console, phases, verbose=args.verbose)
    return 0


def doctor_checks(
    root: Path | None,
    reviewer: bool,
    *,
    context: ContextLoader,
    current_resolver: CurrentResolver,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name in ("git", "python3", "codex"):
        path = shutil.which(name)
        checks.append({
            "section": "Environment", "name": "Python" if name == "python3" else name.capitalize(),
            "status": "pass" if path else "error", "detail": path or "not found",
        })
    if root is None:
        checks.append({"section": "Environment", "name": "Repository", "status": "error", "detail": "not in a Git repository"})
        return checks
    checks.append({"section": "Environment", "name": "Repository", "status": "pass", "detail": str(root)})
    try:
        project, state, workflow = context(root)
        checks.extend([
            {"section": "Workflow", "name": "Project identity", "status": "pass", "detail": project.project_id},
            {"section": "Workflow", "name": "Plan", "status": "pass", "detail": workflow.status},
            {"section": "Workflow", "name": "State", "status": "pass", "detail": state["status"]},
        ])
        phase = current_resolver(workflow, state) if workflow.phases and state.get("current_phase") else None
        if phase:
            checks.append({"section": "Workflow", "name": "Current phase", "status": "pass", "detail": phase.id})
            for dependency in phase.depends_on:
                validate_gate(root, workflow, dependency)
            checks.append({"section": "Workflow", "name": "Previous gates", "status": "pass", "detail": f"{len(phase.depends_on)} dependencies verified"})
        history = audit_history(root, workflow, state)
        checks.append({
            "section": "Workflow", "name": "History integrity", "status": "pass",
            "detail": f"{history['reviews']} reviews, {history['gates']} gates, {history['events']} events",
        })
        writable = os.access(root / ".cw", os.W_OK)
        checks.append({"section": "Security", "name": ".cw writable", "status": "pass" if writable else "error", "detail": "required"})
        snapshot_protected_paths(root, workflow.protected_paths)
        checks.append({"section": "Security", "name": "Protected paths", "status": "pass", "detail": f"{len(workflow.protected_paths)} enforced"})
        session = load_session(root, workflow, phase) if phase else None
        readiness = readiness_path(root)
        owner = session.get("owner_pid") if session else None
        owner_active = isinstance(owner, int) and process_is_alive(owner)
        if session and not owner_active and not readiness.exists():
            raise CwError("A stale implementer session exists", ErrorCode.INVALID_STATE, "Run: cw repair")
        checks.append({
            "section": "Security", "name": "Implementer session",
            "status": "pass" if owner_active else "warning" if session else "neutral",
            "detail": f"active for {session['phase']}" if owner_active else f"detached; review ready for {session['phase']}" if session else "none",
        })
        if readiness.exists():
            if phase is None or session is None:
                raise CwError("Readiness manifest has no active implementer session", ErrorCode.INVALID_STATE, "Run: cw repair")
            manifest = load_readiness(root, phase)
            if manifest["session_id"] != session["session_id"]:
                raise CwError("Readiness manifest does not belong to the active implementer session", ErrorCode.INVALID_STATE, "Run: cw repair")
            checks.append({"section": "Security", "name": "Readiness session", "status": "pass", "detail": phase.id})
        else:
            checks.append({"section": "Security", "name": "Readiness session", "status": "neutral", "detail": "none"})
        checks.append({"section": "Security", "name": ".codex runtime writes", "status": "neutral", "detail": "not required"})
        checks.append({"section": "Security", "name": "Hook trust", "status": "neutral", "detail": "managed by Codex; run /hooks if prompted"})
        if reviewer:
            adapter = CodexAdapter()
            schema = Path(__file__).resolve().parents[2] / "schemas" / "phase-review.schema.json"
            try:
                adapter.smoke_test(root, schema)
                checks.append({"section": "Workflow", "name": "Reviewer connectivity", "status": "pass", "detail": "independent read-only request succeeded"})
            except CwError as exc:
                checks.append({"section": "Workflow", "name": "Reviewer connectivity", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    except CwError as exc:
        checks.append({"section": "Workflow", "name": "Workflow integrity", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    return checks


def command_doctor(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    checks_provider: DoctorProvider,
) -> int:
    try:
        root = root_resolver()
    except CwError:
        root = None
    checks = checks_provider(root, args.reviewer)
    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    passed = sum(item["status"] == "pass" for item in checks)
    payload = {"checks": checks, "result": {"passed": passed, "warnings": warnings, "errors": errors}}
    if args.json:
        emit_json(payload)
    else:
        render_doctor(console, checks, payload["result"], verbose=args.verbose)
    return 1 if errors else 0


def command_error(args: argparse.Namespace, console: Console, *, root_resolver: RootResolver) -> int:
    root = root_resolver()
    record = load_diagnostic(root)
    state_data: dict[str, Any] = {}
    if record is None:
        try:
            loaded = load_json(root / ".cw/state.json")
            state_data = loaded if isinstance(loaded, dict) else {}
            record = legacy_diagnostic(state_data.get("last_error"))
        except CwError:
            record = None
    elif (root / ".cw/state.json").is_file():
        try:
            loaded = load_json(root / ".cw/state.json")
            state_data = loaded if isinstance(loaded, dict) else {}
        except CwError:
            pass
    recovery = state_data.get("infrastructure_error") if isinstance(state_data, dict) else None
    context = {
        "phase": recovery.get("phase") if isinstance(recovery, dict) else state_data.get("current_phase"),
        "retryable": recovery.get("retryable") if isinstance(recovery, dict) else False,
        "operation": recovery.get("operation") if isinstance(recovery, dict) else None,
        "attempt_consumed": False if isinstance(recovery, dict) else None,
    }
    payload = {"error": record, "context": context}
    if args.json:
        emit_json(payload)
    else:
        console.header("Error")
        if record:
            title, detail = error_summary(record["code"], record["message"])
            console.item("✕", title)
            console.field("Type", record["code"])
            if context["phase"]:
                console.field("Phase", context["phase"])
            console.field("Retryable", "YES" if context["retryable"] else "NO")
            if context["attempt_consumed"] is not None:
                console.field("Attempt", "not consumed" if not context["attempt_consumed"] else "consumed")
            if args.verbose:
                console.field("When", record["timestamp"])
                if context["operation"]:
                    console.field("Operation", context["operation"])
            console.line()
            console.section("Summary")
            console.wrapped(detail)
            diagnostic = raw_diagnostic(record)
            if args.raw or record.get("details"):
                console.line()
                console.section("Raw diagnostic")
                console.line("─" * 72)
                console.line(diagnostic)
                console.line("─" * 72)
            if args.verbose and record.get("traceback"):
                console.line()
                console.section("Traceback")
                console.line(str(record["traceback"]))
            if context["retryable"]:
                console.run("cw retry")
            elif record.get("hint"):
                console.run(str(record["hint"]).removeprefix("Run: "))
        else:
            console.item("✓", "No stored workflow error")
    return 1 if record else 0


def command_version(args: argparse.Namespace, console: Console) -> int:
    payload = {"name": "CW", "brand": "CW by Queopius", "product": "Codex Workflow", "version": __version__}
    if args.json:
        emit_json(payload)
    else:
        console.line(f"CW by Queopius {__version__}")
        console.line("Codex Workflow")
    return 0
