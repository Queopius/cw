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
from cw.core.integrity import snapshot_protected_paths
from cw.core.schema import SCHEMA_VERSION
from cw.core.session import load_session, process_is_alive, readiness_path
from cw.core.utils import load_json
from cw.ui.console import Console, emit_json, error_summary


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
    gate_error = None
    gate_error_code = None
    gate_error_details = None
    for phase in workflow.phases:
        exists = gate_path(root, phase.id).is_file()
        gates[phase.id] = exists
        if exists:
            try:
                validate_gate(root, workflow, phase.id)
            except CwError as exc:
                gates[phase.id] = False
                gate_error = str(exc)
                gate_error_code = exc.code.value
                gate_error_details = exc.details
    return {
        "schema_version": SCHEMA_VERSION, "project": project.project_id,
        "repository_root": str(root), "branch": git_branch(root),
        "workflow": "INITIALIZED" if not workflow.phases else "ACTIVE",
        "plan": workflow.status, "state": state["status"], "phase": current,
        "phase_index": index, "phase_count": len(workflow.phases),
        "attempt": state.get("attempt", 0), "max_attempts": workflow.max_review_attempts,
        "ready": (root / ".cw" / "runtime" / "READY_FOR_REVIEW.json").is_file(),
        "gate": gates.get(current, False) if current else False, "gates": gates,
        "gate_error": gate_error, "gate_error_code": gate_error_code,
        "gate_error_details": gate_error_details,
        "phases": [
            {"id": phase.id, "name": phase.name, "depends_on": list(phase.depends_on)}
            for phase in workflow.phases
        ],
        "last_error": state.get("last_error"),
    }


def render_status(console: Console, data: dict[str, Any], verbose: bool = False) -> None:
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
        for index, phase in enumerate(data["phases"]):
            marker = "✓" if data["gates"].get(phase["id"]) else "→" if index == data["phase_index"] else "·"
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
            "section": "Environment", "name": name.capitalize(),
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
            {"section": "Workflow", "name": "phases.yaml", "status": "pass", "detail": workflow.status},
            {"section": "Workflow", "name": "State", "status": "pass", "detail": state["status"]},
        ])
        history = audit_history(root, workflow, state)
        checks.append({
            "section": "Workflow", "name": "History integrity", "status": "pass",
            "detail": f"{history['reviews']} reviews, {history['gates']} gates, {history['events']} events",
        })
        writable = os.access(root / ".cw", os.W_OK)
        checks.append({"section": "Security", "name": ".cw writable", "status": "pass" if writable else "error", "detail": "required"})
        snapshot_protected_paths(root, workflow.protected_paths)
        checks.append({"section": "Security", "name": "Protected paths", "status": "pass", "detail": f"{len(workflow.protected_paths)} enforced"})
        phase = current_resolver(workflow, state) if workflow.phases and state.get("current_phase") else None
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
        checks.append({"section": "Security", "name": ".codex writable", "status": "neutral", "detail": "not required at runtime"})
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


def command_error(args: argparse.Namespace, console: Console, *, root_resolver: RootResolver) -> int:
    root = root_resolver()
    record = load_diagnostic(root)
    if record is None:
        try:
            state = load_json(root / ".cw/state.json")
            record = legacy_diagnostic(state.get("last_error") if isinstance(state, dict) else None)
        except CwError:
            record = None
    payload = {"error": record}
    if args.json:
        emit_json(payload)
    else:
        console.header("Error")
        if record and args.raw:
            console.line(raw_diagnostic(record))
        elif record:
            title, detail = error_summary(record["code"], record["message"])
            console.item("✕", title)
            console.field("Code", record["code"])
            console.field("When", record["timestamp"])
            console.wrapped(detail)
            if record.get("details"):
                console.line()
                console.line("Details")
                console.wrapped(str(record["details"]))
            if args.verbose and record.get("traceback"):
                console.line()
                console.line("Traceback")
                console.line(str(record["traceback"]))
            if record.get("hint"):
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
