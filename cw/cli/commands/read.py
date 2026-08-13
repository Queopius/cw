from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from cw import __version__
from cw.adapters.codex import CodexAdapter
from cw.adapters.invocation import latest_invocation
from cw.adapters.structured_output import codex_schema
from cw.checks.deterministic import load_readiness
from cw.core.audit import audit_history
from cw.core.diagnostics import legacy_diagnostic, load_diagnostic, load_global_diagnostic, raw_diagnostic
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_gate
from cw.core.history import history_timeline
from cw.core.integrity import snapshot_protected_paths
from cw.core.progress import derive_workflow_consistency
from cw.core.schema import SCHEMA_VERSION
from cw.core.session import load_session, process_is_alive, readiness_path
from cw.core.utils import load_json
from cw.update.config import load_update_settings
from cw.update.installation import ManagedInstallation
from cw.integrations.config import project_requirements
from cw.integrations.manager import IntegrationManager
from cw.integrations.models import IntegrationHealth, Requirement
from cw.execution.session import load_batch
from cw.ui.console import Console, emit_json
from cw.ui.renderers import (
    render_doctor,
    render_error,
    render_history,
    render_status as render_status_view,
    render_integrations,
)


RootResolver = Callable[[], Path]
ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]
CurrentResolver = Callable[[Any, dict[str, Any]], Any]
ErrorRecorder = Callable[..., None]
DoctorProvider = Callable[[Path | None, bool, bool, bool], list[dict[str, Any]]]


def git_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=root,
        text=True, capture_output=True, check=False,
    )
    return result.stdout.strip() or "detached HEAD"


def status_payload(root: Path, context: ContextLoader) -> dict[str, Any]:
    project, state, workflow = context(root)
    consistency = derive_workflow_consistency(root, workflow, state) if workflow.phases else None
    current = state.get("current_phase")
    try:
        index = workflow.index(current) if current and workflow.phases else None
    except StopIteration:
        index = None
    gates: dict[str, bool] = {}
    gate_states: dict[str, str] = {}
    invalid_gates: list[str] = []
    gate_error = None
    gate_error_code = None
    gate_error_details = None
    if consistency is not None:
        gate_states.update(consistency.chain.states)
        for phase in workflow.phases:
            gates[phase.id] = gate_states.get(phase.id) == "approved"
        invalid_gates = [phase.id for phase in workflow.phases if gate_states.get(phase.id) == "invalid"]
        if consistency.chain.issues:
            gate_error = "Approval gate chain is invalid"
            gate_error_code = ErrorCode.INVALID_GATE.value
            gate_error_details = "\n".join(consistency.chain.issues)
    batch = load_batch(root)
    if (
        batch and batch.get("status") == "RUNNING"
        and (not isinstance(batch.get("pid"), int) or not process_is_alive(batch["pid"]))
    ):
        batch = {**batch, "status": "INTERRUPTED"}
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
                "objective": phase.objective,
                "depends_on": list(phase.depends_on),
            }
            for phase in workflow.phases
        ],
        "last_error": state.get("last_error"),
        "infrastructure_error": state.get("infrastructure_error"),
        "batch": batch,
        "consistent": consistency.consistent if consistency is not None else True,
        "consistency_issues": list(consistency.issues) if consistency is not None else [],
        "expected_phase": consistency.expected_current if consistency is not None else None,
        "approved_through": consistency.chain.approved[-1][0] if consistency and consistency.chain.approved else None,
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
    if not data.get("consistent", True):
        record_error(
            CwError(
                "Workflow state is inconsistent with approval evidence",
                ErrorCode.INVALID_STATE,
                "Run: cw repair",
                details="\n".join(data.get("consistency_issues", [])),
            ),
            source="status",
        )
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
    return 1 if not data.get("consistent", True) or data["state"] == "ERROR" or data.get("gate_error") else 0


def command_explain(
    args: argparse.Namespace, console: Console, *, root_resolver: RootResolver, context: ContextLoader,
) -> int:
    root = root_resolver()
    data = status_payload(root, context)
    payload = {
        "consistent": data.get("consistent", True),
        "current_phase": data.get("phase"),
        "expected_phase": data.get("expected_phase"),
        "approved_through": data.get("approved_through"),
        "issues": data.get("consistency_issues", []),
        "recovery": "cw repair" if not data.get("consistent", True) else None,
    }
    if args.json:
        emit_json(payload)
        return 1 if not payload["consistent"] else 0
    console.header("Explain")
    if payload["consistent"]:
        console.item("✓", "Workflow state is consistent")
        console.wrapped("Configured phases, approval gates and state agree.", 2)
        return 0
    console.item("✕", "Why is the workflow blocked?")
    console.wrapped(
        f"CW found validated approval evidence through {payload['approved_through'] or 'no phase'}, "
        f"but state.json points to {payload['current_phase'] or 'no phase'}.",
        2,
    )
    console.line()
    console.wrapped("No approved work will be discarded.", 2)
    console.line()
    console.subsection("Safe recovery")
    console.action("cw repair", "Reconcile state from validated evidence")
    return 1


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
    integrations: bool = False,
    codex: bool = False,
    *,
    context: ContextLoader,
    current_resolver: CurrentResolver,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    phase = None
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
        consistency = derive_workflow_consistency(root, workflow, state) if workflow.phases else None
        if consistency is not None:
            approved_count = len(consistency.chain.approved)
            checks.extend([
                {
                    "section": "Workflow consistency", "name": "Gate chain",
                    "status": "pass" if not consistency.chain.issues else "error",
                    "detail": f"{approved_count} contiguous gates" if not consistency.chain.issues else consistency.chain.issues[0],
                },
                {
                    "section": "Workflow consistency", "name": "Current phase",
                    "status": "pass" if state.get("current_phase") == consistency.expected_current else "error",
                    "detail": str(consistency.expected_current or "workflow complete"),
                },
                {
                    "section": "Workflow consistency", "name": "Last gate",
                    "status": "pass" if state.get("last_gate") == consistency.expected_last_gate else "error",
                    "detail": str(consistency.expected_last_gate or "none"),
                },
                {
                    "section": "Workflow consistency", "name": "Readiness",
                    "status": "error" if any("readiness belongs" in issue for issue in consistency.issues) else "pass",
                    "detail": "belongs to current phase" if readiness_path(root).is_file() else "not ready",
                },
                {
                    "section": "Workflow consistency", "name": "State and evidence",
                    "status": "pass" if consistency.consistent else "error",
                    "detail": "consistent" if consistency.consistent else consistency.issues[0],
                },
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
        hooks_file = root / ".codex/hooks.json"
        hook_script = root / ".codex/hooks/phase_gate.py"
        try:
            hooks_document = load_json(hooks_file)
            stop_configured = isinstance(hooks_document, dict) and bool(hooks_document.get("hooks", {}).get("Stop"))
        except CwError:
            stop_configured = False
        checks.append({
            "section": "Hooks", "name": "CW Stop hook",
            "status": "pass" if stop_configured and hook_script.is_file() else "error",
            "detail": "configured" if stop_configured and hook_script.is_file() else "missing or invalid",
        })
        checks.append({
            "section": "Hooks", "name": "External lifecycle hooks",
            "status": "neutral", "detail": "user-owned; not required by CW",
        })
    except CwError as exc:
        checks.append({"section": "Workflow", "name": "Workflow integrity", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    if reviewer:
        adapter = CodexAdapter()
        schema = codex_schema("review-output.schema.json")
        try:
            adapter.smoke_test(root, schema)
            checks.append({"section": "Workflow", "name": "Reviewer connectivity", "status": "pass", "detail": "independent read-only request succeeded"})
        except CwError as exc:
            checks.append({"section": "Workflow", "name": "Reviewer connectivity", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    if integrations:
        try:
            required = project_requirements(root)
            if phase is not None:
                required |= set(phase.required_integrations)
            integration_check = IntegrationManager().check(root, required=required, force=True)
            for integration in integration_check.integrations:
                healthy = integration.health is IntegrationHealth.AVAILABLE
                blocking = integration.required is Requirement.REQUIRED and not healthy
                detail = f"{integration.health.value.replace('_', ' ').title()} · {'required' if blocking else 'optional'} · impact {integration.impact.lower()}"
                if integration.http_status:
                    detail += f" · HTTP {integration.http_status}"
                checks.append({
                    "section": "Integrations", "name": f"{integration.id.title()} MCP",
                    "status": "pass" if healthy else "error" if blocking else "warning",
                    "detail": detail,
                })
        except CwError as exc:
            checks.append({"section": "Integrations", "name": "Integration health", "status": "error", "detail": f"{exc.code.value}: {exc.message}"})
    if codex:
        invocation = latest_invocation(root)
        if invocation is None:
            checks.append({
                "section": "Managed Codex", "name": "Latest invocation",
                "status": "neutral", "detail": "none recorded",
            })
        else:
            command = str(invocation.get("command", ""))
            unsafe = "mcp_servers." in command
            checks.append({
                "section": "Managed Codex", "name": "Sanitized argv",
                "status": "error" if unsafe else "pass", "detail": command,
            })
            checks.append({
                "section": "Managed Codex", "name": "MCP overrides",
                "status": "error" if unsafe else "pass",
                "detail": "unsupported mcp_servers.* override detected" if unsafe else "none",
            })
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
    checks = checks_provider(root, args.reviewer, args.integrations, args.codex)
    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    passed = sum(item["status"] == "pass" for item in checks)
    payload = {"checks": checks, "result": {"passed": passed, "warnings": warnings, "errors": errors}}
    if args.json:
        emit_json(payload)
    else:
        render_doctor(console, checks, payload["result"], verbose=args.verbose)
    return 1 if errors else 0


def command_integrations(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    required = project_requirements(root)
    if workflow.phases and state.get("current_phase"):
        required |= set(workflow.phase(str(state["current_phase"])).required_integrations)
    manager = IntegrationManager()
    result = manager.check(root, required=required, force=True) if args.action == "check" else None
    integrations = result.integrations if result else manager.configured(required)
    if args.action == "info":
        if not args.name:
            raise CwError("Integration name is required", ErrorCode.USAGE_ERROR, "Run: cw integrations info <name>", exit_code=2)
        integrations = tuple(item for item in integrations if item.id == args.name)
        if not integrations:
            raise CwError(f"Integration is not configured: {args.name}", ErrorCode.MCP_NOT_CONFIGURED, exit_code=3)
    payload = {
        "project": workflow.id,
        "checked": result is not None,
        "integrations": [item.to_dict() for item in integrations],
        "workflow_can_continue": all(item.impact != "BLOCKING" for item in integrations),
    }
    if args.json:
        emit_json(payload)
    else:
        render_integrations(console, payload, verbose=args.verbose, raw=result.stderr if result else "")
    return 0 if payload["workflow_can_continue"] else 3


def command_error(args: argparse.Namespace, console: Console, *, root_resolver: RootResolver) -> int:
    try:
        root = root_resolver()
    except CwError:
        root = None
    record = load_diagnostic(root) if root is not None else None
    state_data: dict[str, Any] = {}
    if record is None and root is not None:
        try:
            loaded = load_json(root / ".cw/state.json")
            state_data = loaded if isinstance(loaded, dict) else {}
            record = legacy_diagnostic(state_data.get("last_error"))
        except CwError:
            record = None
    elif root is not None and (root / ".cw/state.json").is_file():
        try:
            loaded = load_json(root / ".cw/state.json")
            state_data = loaded if isinstance(loaded, dict) else {}
        except CwError:
            pass
    global_record = load_global_diagnostic()
    if global_record is not None and (
        record is None or (
            global_record.get("source") == "update"
            and str(global_record.get("timestamp", "")) > str(record.get("timestamp", ""))
        )
    ):
        record = global_record
    recovery = state_data.get("infrastructure_error") if isinstance(state_data, dict) else None
    phase_id = recovery.get("phase") if isinstance(recovery, dict) else state_data.get("current_phase")
    phase_label = phase_id
    try:
        from cw.core.workflow import load_workflow

        if root is None:
            raise CwError("No project context", ErrorCode.WORKFLOW_ERROR)
        workflow = load_workflow(root)
        if phase_id:
            phase = workflow.phase(str(phase_id))
            phase_label = f"{phase.id.split('-', 1)[0]} · {phase.name}"
    except (CwError, KeyError):
        pass
    retryable_update_codes = {
        ErrorCode.UPDATE_CHECK_ERROR.value, ErrorCode.UPDATE_DOWNLOAD_ERROR.value,
        ErrorCode.UPDATE_CHECKSUM_ERROR.value, ErrorCode.UPDATE_INSTALL_ERROR.value,
        ErrorCode.UPDATE_SMOKE_TEST_ERROR.value, ErrorCode.UPDATE_ROLLBACK_ERROR.value,
    }
    context = {
        "phase": phase_id,
        "phase_label": phase_label,
        "retryable": recovery.get("retryable") if isinstance(recovery, dict) else bool(record and record.get("code") in retryable_update_codes),
        "operation": recovery.get("operation") if isinstance(recovery, dict) else None,
        "attempt_consumed": False if isinstance(recovery, dict) else None,
        "diagnostic": raw_diagnostic(record) if record else "",
        "update_safety": bool(record and str(record.get("code", "")).startswith("UPDATE_")),
    }
    payload = {"error": record, "context": context}
    if args.json:
        emit_json(payload)
    else:
        render_error(console, record, context, verbose=args.verbose, raw=args.raw)
    return 1 if record else 0


def command_version(args: argparse.Namespace, console: Console) -> int:
    from cw.core.build import version_diagnostics

    settings = load_update_settings()
    installation = ManagedInstallation()
    payload = {
        "name": "CW", "brand": "CW by Queopius", "product": "Codex Workflow",
        "version": __version__, "channel": settings.channel, "schema": SCHEMA_VERSION,
        "install": installation.kind,
    }
    diagnostics = version_diagnostics()
    payload.update(diagnostics)
    if args.json:
        emit_json(payload)
    else:
        console.line(f"CW {__version__}")
        console.line("Codex Workflow · by Queopius")
        console.line()
        console.field("Channel", settings.channel)
        console.field("Schema", SCHEMA_VERSION)
        console.field("Install", installation.kind)
        if args.verbose:
            console.field("Executable", diagnostics["executable"])
            console.field("Runtime", diagnostics["runtime"])
            console.field("Build", diagnostics["build"])
            if diagnostics["source_build"]:
                console.field("Source", diagnostics["source_build"])
                console.field("Source match", "YES" if diagnostics["source_match"] else "NO · stale installation")
    return 0
