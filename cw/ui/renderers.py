from __future__ import annotations

from typing import Any

from cw.core.severity import CriterionSeverity
from cw.execution.duration import format_duration

from .console import Console, error_summary
from .layout import display_state
from .progress import progress_bar
from .symbols import ACTIVE, ERROR, PENDING, SUCCESS, WARNING


def _phase_title(phase: Any) -> str:
    identifier = phase.id if hasattr(phase, "id") else str(phase["id"])
    name = phase.name if hasattr(phase, "name") else str(phase["name"])
    return f"{identifier.split('-', 1)[0]} · {name}"


def _metric(console: Console, label: str, value: str, width: int = 12) -> None:
    prefix = f"  {label.ljust(width)}"
    console.line(prefix + console.state(value))


def _progress(console: Console, approved: int, total: int) -> None:
    suffix_width = 7
    bar_width = max(4, min(30, console.width - len("  Progress    ") - suffix_width))
    bar = progress_bar(approved, total, bar_width, unicode=console.color)
    completed = console.style(bar.complete, "32") if console.color else bar.complete
    remaining = console.style(bar.remaining, "2") if console.color else bar.remaining
    console.line(f"  Progress    {completed}{remaining}  {bar.percentage:>3}%")


def _timeline(console: Console, data: dict[str, Any]) -> None:
    current_id = data.get("phase")
    for index, phase in enumerate(data["phases"]):
        gate_state = data["gate_states"].get(phase["id"], "pending")
        marker = (
            WARNING if gate_state == "invalid"
            else SUCCESS if gate_state == "approved"
            else ACTIVE if phase["id"] == current_id
            else PENDING
        )
        if phase["id"] == current_id and index:
            console.line()
        console.phase(marker, phase["number"], phase["name"], indent=4)
        if phase["id"] == current_id and index + 1 < len(data["phases"]):
            console.line()


def _status_actions(console: Console, data: dict[str, Any]) -> None:
    console.line()
    batch = data.get("batch")
    if isinstance(batch, dict) and batch.get("status") in {"INTERRUPTED", "STOPPED"}:
        console.action("cw run --resume", "Resume the interrupted batch budget")
    managed_run = data.get("run")
    if isinstance(managed_run, dict) and managed_run.get("alive"):
        console.action("cw inspect session", "Inspect the active Codex execution")
        console.action(f"cw logs --run {managed_run.get('run_id')}", "View structured execution events")
        return
    if isinstance(managed_run, dict) and managed_run.get("stale"):
        console.action("cw inspect session", "Inspect the interrupted execution")
        console.action("cw repair", "Archive the stale run and resume safely")
        return
    if data.get("gate_error"):
        console.action("cw error", "View gate integrity details")
        console.action("cw repair", "Repair workflow metadata")
        return
    if data["state"] == "ERROR":
        console.action("cw error", "View failure details")
        if isinstance(data.get("infrastructure_error"), dict) and data["infrastructure_error"].get("retryable"):
            console.action("cw retry", "Retry the failed operation")
        console.action("cw doctor", "Check environment and integration")
        return
    if data["state"] == "HUMAN_REVIEW_REQUIRED":
        console.action("cw history", "Review the phase decision")
        console.action("cw review --human-approve", "Approve the human gate")
        return
    if data["state"] == "COMPLETED":
        console.action("cw history", "View the complete audit trail")
        return
    if data["state"] in {"PLANNED_COMPLETE", "COMPLETION_REVIEW", "COMPLETION_BLOCKED"}:
        console.action("cw completion review", "Run or retry independent completion review")
        console.action("cw completion show", "Inspect Completion Contract evidence")
        return
    if data["state"] == "EXTENSION_PROPOSED":
        console.action("cw completion show", "Inspect the proposed extension")
        console.action("cw completion approve", "Authorize the extension")
        console.action("cw completion reject", "Reject without changing phases")
        return
    console.action("cw", "Continue development")
    console.action("cw validate", "Validate current phase")
    console.action("cw history", "View audit trail")


def _render_completed(console: Console, data: dict[str, Any], *, verbose: bool) -> None:
    console.header(version=True)
    console.aligned(data["project"], data["branch"], left_style="1")
    console.rule()
    console.line()
    title = "COMPLETION TARGET SATISFIED" if data.get("completion_mode") == "CONTRACT_AWARE" else "WORKFLOW COMPLETE"
    console.line(f"  {console.style(SUCCESS, '32')} {console.style(title, '1;32')}")
    console.line()
    _progress(console, data["approved_count"], data["phase_count"])
    console.line()
    _metric(console, "Approved", f"{data['approved_count']} / {data['phase_count']} phases")
    _metric(console, "State", "COMPLETED")
    _metric(console, "Plan", str(data["plan"]))
    console.line()
    for phase in data["phases"]:
        marker = SUCCESS if data["gate_states"].get(phase["id"]) == "approved" else WARNING
        console.phase(marker, phase["number"], phase["name"], indent=4)
    console.line()
    console.rule()
    console.wrapped(f"{data['approved_count']} approved · 0 active · 0 remaining", 2)
    console.line()
    if data.get("completion_mode") == "CONTRACT_AWARE":
        console.wrapped("All authorized phase gates and completion evidence are valid.", 2)
    else:
        console.wrapped("All configured gates are valid.", 2)
    _status_actions(console, data)
    if verbose:
        _status_details(console, data)


def _status_details(console: Console, data: dict[str, Any]) -> None:
    console.line()
    console.subsection("Details")
    console.rule()
    console.field("Root", data["repository_root"])
    console.field("State file", ".cw/state.json")
    console.field("Plan file", ".codex/workflow/phases.yaml")
    if data.get("invalid_gates"):
        console.field("Invalid gates", ", ".join(data["invalid_gates"]))


def _render_no_plan(console: Console, data: dict[str, Any], *, verbose: bool) -> None:
    console.header(version=True)
    console.aligned(data["project"], data["branch"], left_style="1")
    console.rule()
    console.line()
    if data["state"] == "ERROR":
        marker, health, code = ERROR, "ERROR", "31"
    elif data["state"] == "PLANNING":
        marker, health, code = ACTIVE, "PLANNING", "36"
    else:
        marker, health, code = "○", "INITIALIZED", "36"
    console.aligned("WORKFLOW", f"{marker} {health}", left_style="1", right_style=code)
    console.line()
    _metric(console, "Plan", "NOT CREATED")
    console.line()
    console.wrapped("No development plan exists yet.", 2)
    if data["state"] == "ERROR":
        recovery = data.get("infrastructure_error")
        error_code = str(recovery.get("error_code", "")) if isinstance(recovery, dict) else ""
        title, detail = error_summary(error_code, str(data.get("last_error") or ""))
        console.line()
        console.line(f"  {console.style(ERROR, '31')} {console.style(title, '1;31')}")
        console.wrapped(detail, 4)
        console.line()
        console.subsection("Next step")
        console.rule()
        console.line()
        if isinstance(recovery, dict) and recovery.get("retryable") is True:
            console.action("cw retry", "Retry plan generation")
        console.action("cw error", "View failure details")
    else:
        console.line()
        console.subsection("Next step")
        console.rule()
        console.line()
        console.action("cw plan", "Analyze this project and create a plan")
    if verbose:
        _status_details(console, data)


def _render_planned_complete(console: Console, data: dict[str, Any], *, verbose: bool) -> None:
    console.header(version=True)
    console.aligned(data["project"], data["branch"], left_style="1")
    console.rule()
    console.line()
    console.line(f"  {console.style(SUCCESS, '32')} {console.style('PLANNED SCOPE COMPLETE', '1;32')}")
    console.line()
    _metric(console, "Gates", f"{data['approved_count']} / {data['phase_count']} valid", 18)
    _metric(console, "Planned scope", "COMPLETE", 18)
    target = data.get("completion_target") or {}
    console.line()
    console.subsection("Completion")
    console.rule()
    console.line()
    _metric(console, "Target", str(target.get("name", "NOT DECLARED")), 18)
    review = data.get("completion_review") or {}
    _metric(console, "Review", str(review.get("decision", "PENDING")).replace("_", " "), 18)
    _metric(
        console, "Satisfied",
        f"{data.get('completion_verified_count', 0)} / {data.get('completion_requirement_count', 0)} requirements",
        18,
    )
    proposal = data.get("extension_proposal")
    if isinstance(proposal, dict):
        console.line()
        console.subsection("Extension")
        console.rule()
        console.line()
        _metric(console, "Proposed", f"{len(proposal.get('phases', []))} phases", 18)
        _metric(console, "Authorization", "REQUIRED", 18)
        console.wrapped("CW may recommend more work. Only the human may authorize more work.", 2)
    elif data["state"] == "COMPLETION_BLOCKED":
        console.line()
        console.wrapped(f"{WARNING} Completion review is blocked and retryable; product failure was not inferred.", 2)
    _status_actions(console, data)
    if verbose:
        _status_details(console, data)


def render_status(console: Console, data: dict[str, Any], *, verbose: bool = False) -> None:
    if not data.get("consistent", True):
        console.header("Workflow Integrity")
        console.line()
        console.line(f"  {console.style(ERROR, '31')} {console.style('STATE INCONSISTENT', '1;31')}")
        console.line()
        _metric(console, "Current phase", str(data.get("phase") or "none"), 18)
        _metric(console, "Valid gates", f"through {data.get('approved_through') or 'none'}", 18)
        _metric(console, "Expected phase", str(data.get("expected_phase") or "workflow complete"), 18)
        console.line()
        console.wrapped("CW will not continue with contradictory state.", 2)
        if data.get("invalid_gates"):
            console.line()
            console.line(f"  {console.style(WARNING, '33')} {console.style('Approval gate invalidated', '1;33')}")
            for phase_id in data["invalid_gates"]:
                phase = next((item for item in data.get("phases", []) if item["id"] == phase_id), None)
                label = f"{phase['number']}  {phase['name']}" if phase else phase_id
                console.wrapped(f"{WARNING} {label}", 4)
        if verbose:
            console.line()
            console.subsection("Consistency details")
            for issue in data.get("consistency_issues", []):
                console.wrapped(f"{WARNING} {issue}", 2)
        console.line()
        console.run("cw repair")
        return
    if not data["phases"]:
        _render_no_plan(console, data, verbose=verbose)
        return
    if (
        data.get("completion_mode") == "CONTRACT_AWARE"
        and data.get("planned_scope_complete")
        and not data.get("completion_satisfied")
        and not data.get("gate_error")
    ):
        _render_planned_complete(console, data, verbose=verbose)
        return
    if (
        data["state"] == "COMPLETED"
        and data["phases"]
        and not data.get("gate_error")
        and data["approved_count"] == data["phase_count"]
    ):
        _render_completed(console, data, verbose=verbose)
        return

    console.header(version=True)
    console.aligned(data["project"], data["branch"], left_style="1")
    console.rule()
    console.line()

    health = "ERROR" if data["state"] == "ERROR" or data.get("gate_error") else "ATTENTION" if data["state"] == "HUMAN_REVIEW_REQUIRED" else "ACTIVE"
    health_symbol = ERROR if health == "ERROR" else WARNING if health == "ATTENTION" else "●"
    health_code = "31" if health == "ERROR" else "33" if health == "ATTENTION" else "36"
    console.aligned("WORKFLOW", f"{health_symbol} {health}", left_style="1", right_style=health_code)
    console.line()
    _progress(console, data["approved_count"], data["phase_count"])
    _metric(console, "Approved", f"{data['approved_count']} / {data['phase_count']} phases")
    _metric(console, "State", display_state(data["state"]))
    _metric(console, "Plan", display_state(data["plan"]))

    if data.get("phase"):
        current = data["phases"][data["phase_index"]]
        console.line()
        console.subsection("Current phase")
        console.rule()
        console.line()
        console.focus(ACTIVE, current["number"], current["name"], indent=2)
        objective = current.get("objective")
        if objective and objective.strip() not in {current["name"].strip(), f"Deliver {current['name']}"}:
            console.line()
            console.wrapped(objective, 4)
        console.line()
        _metric(console, "Position", f"{data['position']} / {data['phase_count']}", 14)
        _metric(console, "Attempt", f"{data['attempt']} / {data['max_attempts']}", 14)
        _metric(console, "Readiness", "READY" if data["ready"] else "NOT READY", 14)
        _metric(console, "Gate", "APPROVED" if data["gate"] else "PENDING", 14)

        console.line()
        console.subsection("Development plan")
        console.rule()
        console.line()
        _timeline(console, data)
        console.line()
        console.rule()
        remaining = max(0, data["phase_count"] - data["approved_count"] - 1)
        active = 0 if data["state"] == "COMPLETED" else 1
        summary = f"{data['approved_count']} approved · {active} active · {remaining} remaining"
        console.wrapped(summary, 2)
    if data.get("gate_error"):
        console.line()
        console.line(f"  {console.style(WARNING, '33')} {console.style('Approval gate invalidated', '1;33')}")
        console.wrapped("An approved artifact has changed since review.", 4)
    elif data["state"] == "ERROR":
        recovery = data.get("infrastructure_error")
        code = str(recovery.get("error_code", "")) if isinstance(recovery, dict) else str(data.get("last_error") or "").split(":", 1)[0]
        title, detail = error_summary(code, str(data.get("last_error") or ""))
        console.line()
        console.line(f"  {console.style(ERROR, '31')} {console.style('WORKFLOW BLOCKED', '1;31')}")
        console.wrapped(title, 4)
        console.wrapped(detail, 4)
    elif data["state"] == "HUMAN_REVIEW_REQUIRED":
        console.line()
        console.line(f"  {console.style(WARNING, '33')} {console.style('HUMAN REVIEW REQUIRED', '1;33')}")
        console.wrapped("CW cannot safely continue automatically.", 4)

    batch = data.get("batch")
    if isinstance(batch, dict) and batch.get("status") not in {None, "COMPLETED"}:
        console.line()
        console.subsection("Batch")
        console.rule()
        console.line()
        _metric(console, "Status", str(batch.get("status", "UNKNOWN")).replace("_", " "), 14)
        _metric(console, "Completed", f"{batch.get('completed_phases', 0)} / {batch.get('requested_phases', 0)}", 14)
        remaining = max(0, int(batch.get("requested_phases", 0)) - int(batch.get("completed_phases", 0)))
        _metric(console, "Remaining", str(remaining), 14)

    managed_run = data.get("run")
    if isinstance(managed_run, dict):
        console.line()
        console.subsection("Execution")
        console.rule()
        console.line()
        _metric(console, "Run", str(managed_run.get("run_id", "unknown")), 14)
        run_state = "INTERRUPTED" if managed_run.get("stale") else str(managed_run.get("status", "UNKNOWN")).replace("_", " ")
        _metric(console, "State", run_state, 14)
        _metric(console, "Activity", str(managed_run.get("last_activity", "Codex working")), 14)
        if managed_run.get("stale"):
            console.line()
            console.wrapped(f"{WARNING} The CW supervisor and managed Codex process are no longer running.", 2)

    _status_actions(console, data)
    if verbose:
        _status_details(console, data)


def render_history(console: Console, phases: list[dict[str, Any]], *, verbose: bool = False) -> None:
    console.header("History")
    if not phases:
        console.wrapped(f"{PENDING} No workflow evidence recorded", 2)
        return
    labels = {
        "approved": "Approved",
        "revision_required": "Revision required",
        "infrastructure_failure": "Infrastructure failure",
        "infrastructure_failure_recovered": "Infrastructure failure recovered",
        "human_review_required": "Human review required",
        "current": "Current",
    }
    for phase_index, phase in enumerate(phases):
        marker = SUCCESS if phase["approved"] else ACTIVE if phase["current"] else WARNING
        console.focus(marker, phase["number"], phase["name"], indent=2)
        for entry in phase["entries"]:
            label = labels[entry["kind"]]
            attempt = entry.get("attempt")
            summary = f"{label} · attempt {attempt}" if attempt else label
            console.wrapped(summary, 6)
            if verbose:
                if entry.get("timestamp"):
                    console.field("Timestamp", entry["timestamp"], 12)
                if entry.get("review"):
                    console.field("Review", entry["review"], 12)
                if entry.get("gate"):
                    console.field("Gate", entry["gate"], 12)
                if entry.get("error_code"):
                    console.field("Error", entry["error_code"], 12)
        if phase_index < len(phases) - 1:
            console.line()


def render_doctor(console: Console, checks: list[dict[str, Any]], result: dict[str, int], *, verbose: bool = False) -> None:
    console.header("Doctor")
    sections = list(dict.fromkeys(check["section"] for check in checks))
    ordered = [section for section in ("Environment", "Workflow", "Security") if section in sections]
    ordered.extend(section for section in sections if section not in ordered)
    for section_index, section in enumerate(ordered):
        if section_index:
            console.line()
        console.line(f"  {console.style(section, '1')}")
        for check in (item for item in checks if item["section"] == section):
            marker = {"pass": SUCCESS, "warning": WARNING, "error": ERROR, "neutral": PENDING}[check["status"]]
            detail = str(check["detail"])
            original_name = str(check["name"])
            name = {
                ".cw writable": "Runtime writable",
                ".codex runtime writes": ".codex runtime writes not required",
                "Hook trust": "Hook trust managed by Codex",
            }.get(original_name, original_name)
            if original_name in {".cw writable", ".codex runtime writes", "Hook trust"}:
                detail = ""
            show_inline = section == "Environment"
            if show_inline and len(name) + len(detail) + 10 <= console.width:
                left = f"{marker} {name.ljust(16)}"
                gap = " " if len(name) >= 16 else ""
                console.line("  " + console.style(left, {SUCCESS: '32', WARNING: '33', ERROR: '31', PENDING: '2'}[marker]) + gap + detail)
            else:
                console.line("  " + console.style(marker, {SUCCESS: '32', WARNING: '33', ERROR: '31', PENDING: '2'}[marker]) + f" {name}")
                if detail and (verbose or check["status"] in {"warning", "error", "neutral"} or original_name == "Reviewer connectivity"):
                    console.wrapped(detail, 6)
    console.line()
    console.rule()
    console.line()
    if result["errors"]:
        console.line(f"  {console.style(ERROR, '31')} {console.style('Needs attention', '1;31')}")
    elif result["warnings"]:
        console.line(f"  {console.style(WARNING, '33')} {console.style('Healthy with warnings', '1;33')}")
    else:
        console.line(f"  {console.style(SUCCESS, '32')} {console.style('Healthy', '1;32')}")
    console.wrapped(
        f"{result['passed']} checks passed · {result['warnings']} warnings · {result['errors']} errors",
        4,
    )


def render_start(console: Console, data: dict[str, Any]) -> None:
    console.line(console.style(f"CW · {data['project']}", "1;36"))
    console.line()
    console.focus(ACTIVE, data["number"], data["name"], indent=0)
    console.line()
    _metric(console, "Progress", f"{data['approved']} / {data['total']} approved", 16)
    _metric(console, "Attempt", f"{data['attempt']} / {data['max_attempts']}", 16)
    _metric(console, "Implementer", "workspace-write", 16)
    _metric(console, "Reviewer", "read-only", 16)
    console.line()
    console.line(f"{console.style(SUCCESS, '32')} Workflow healthy")
    console.line(f"{console.style(SUCCESS, '32')} Previous gates verified")
    console.line(f"{console.style(SUCCESS, '32')} Project ready")
    console.line()
    console.line(console.style(f"{ACTIVE} Starting Codex session…", "36"))


def render_completed_action(
    console: Console,
    workflow: Any,
    *,
    title: str = "Codex Workflow",
    detail: str = "All configured approval gates are valid. No implementation session was started.",
) -> None:
    console.header(title)
    console.line(f"  {console.style(SUCCESS, '32')} {console.style('WORKFLOW COMPLETE', '1;32')}")
    console.line()
    console.field("Approved", f"{len(workflow.phases)} / {len(workflow.phases)} phases")
    console.wrapped(detail, 2)


def render_completed_start(console: Console, workflow: Any) -> None:
    render_completed_action(console, workflow)
    console.line()
    console.action("cw status", "View the completed workflow")
    console.action("cw history", "View the audit trail")


def render_transition(console: Console, approved: Any, following: Any | None) -> None:
    console.line()
    console.line(f"{console.style(SUCCESS, '32')} {console.style(f'Phase {_phase_title(approved)}', '1')}")
    console.line()
    _metric(console, "Validation", "PASSED", 16)
    _metric(console, "Review", "APPROVED", 16)
    _metric(console, "Gate", "VERIFIED", 16)
    console.line()
    console.rule(indent=0)
    console.line()
    if following is None:
        console.line(f"{console.style(SUCCESS, '32')} {console.style('Workflow complete', '1')}")
    else:
        console.focus(ACTIVE, following.id.split("-", 1)[0], following.name, indent=0)
        console.line()
        console.line("Continuing…")


def render_validation(console: Console, phase: Any, result: Any, *, verbose: bool = False) -> None:
    console.header("Validate")
    console.focus(ACTIVE, phase.id.split("-", 1)[0], phase.name, indent=0)
    console.line()
    for check in result.checks:
        passed = check.get("status") != "failed" and check.get("exit_code", 0) == 0
        name = str(check["name"]).replace("Previous gates", "Dependency gates")
        if check.get("command"):
            name = f"{name} · {check['command']}"
        console.line(f"  {console.style(SUCCESS if passed else ERROR, '32' if passed else '31')} {name}")
        if verbose and check.get("detail"):
            console.wrapped(str(check["detail"]), 6)
    console.line()
    if result.passed:
        console.line(console.style("Validation passed.", "1;32"))
    else:
        console.line(console.style("Validation failed.", "1;31"))
        for error in result.errors[:3]:
            console.wrapped(str(error), 2)
        console.run("cw error")


def render_review_start(console: Console, phase: Any) -> None:
    console.header("Review")
    console.focus(ACTIVE, phase.id.split("-", 1)[0], phase.name, indent=0)
    console.line()
    console.line(f"  {console.style(ACTIVE, '36')} Running deterministic validation and independent reviewer…")


def render_review_result(console: Console, phase: Any, report: dict[str, Any], workflow: Any, *, include_header: bool = True) -> None:
    if include_header:
        console.header("Review")
        console.focus(ACTIVE, phase.id.split("-", 1)[0], phase.name, indent=0)
        console.line()
    else:
        console.line()
    console.line(f"  {console.style(SUCCESS, '32')} Manifest")
    console.line(f"  {console.style(SUCCESS, '32')} Deterministic checks")
    console.line(f"  {console.style(SUCCESS, '32')} Dependency gates")
    console.line()
    decision = report["decision"]
    if decision == "APPROVE" and (not phase.requires_human_approval or report.get("human")):
        console.line(console.style(f"{SUCCESS} APPROVED", "1;32"))
        configured = {criterion.id: criterion for criterion in phase.acceptance_criteria}
        advisory = [
            result for result in report.get("criteria", [])
            if result.get("status") != "PASS"
            and configured.get(result.get("id")) is not None
            and configured[result["id"]].severity == CriterionSeverity.ADVISORY
        ]
        for observation in advisory:
            console.wrapped(f"{WARNING} {observation['id']} · advisory observation", 2)
        console.line()
        _metric(console, "Gate", f"{phase.id}.approved.json")
        next_id = report.get("next_phase")
        if next_id:
            _metric(console, "Next", _phase_title(workflow.phase(next_id)))
        elif report.get("workflow_completed"):
            _metric(console, "State", "COMPLETED")
    elif decision == "REVISE":
        console.line(console.style(f"{ERROR} REVISION REQUIRED", "1;31"))
        console.line()
        issues = report.get("blocking_issues", [])
        _metric(console, "Phase", _phase_title(phase))
        _metric(console, "Issues", f"{len(issues)} blocking")
        console.line()
        for issue in issues:
            console.wrapped(str(issue), 2)
            console.line()
        console.wrapped(f"Phase {phase.id.split('-', 1)[0]} remains active.", 0)
        console.run("cw")
    else:
        console.line(console.style(f"{WARNING} HUMAN REVIEW REQUIRED", "1;33"))
        console.line()
        _metric(console, "Phase", _phase_title(phase))
        console.wrapped("CW cannot safely continue automatically.", 2)


def render_error(console: Console, record: dict[str, Any] | None, context: dict[str, Any], *, verbose: bool = False, raw: bool = False) -> None:
    console.header("Error")
    if not record:
        console.line(console.style(f"{SUCCESS} No stored workflow error", "32"))
        return
    title, detail = error_summary(record["code"], record["message"])
    console.line(f"  {console.style(record['code'], '1;31')}")
    console.line()
    if context.get("phase_label"):
        _metric(console, "Phase", str(context["phase_label"]))
    _metric(console, "Retryable", "YES" if context.get("retryable") else "NO")
    if context.get("attempt_consumed") is not None:
        _metric(console, "Attempt", "consumed" if context["attempt_consumed"] else "not consumed")
    if verbose:
        _metric(console, "Timestamp", str(record.get("timestamp") or "unknown"))
        if context.get("operation"):
            _metric(console, "Operation", str(context["operation"]))
    console.line()
    console.line("  " + console.style("Summary", "1"))
    console.wrapped(title, 2)
    console.wrapped(detail, 2)
    if context.get("update_safety"):
        console.line()
        console.line("  " + console.style("Safety", "1"))
        console.wrapped("The previous CW version remains active. No project files were changed.", 2)
    console.line()
    console.line("  " + console.style("Next", "1"))
    if context.get("retryable"):
        console.wrapped("cw retry", 2)
    elif record.get("hint"):
        console.wrapped(str(record["hint"]).removeprefix("Run: "), 2)
    diagnostic = str(context.get("diagnostic") or "")
    if raw or record.get("details"):
        console.line()
        console.rule()
        console.line()
        console.line("  " + console.style("Raw diagnostic", "1"))
        console.line()
        for line in diagnostic.splitlines() or [""]:
            console.wrapped(line, 2)
    if verbose and record.get("traceback"):
        console.line()
        console.line("  " + console.style("Traceback", "1"))
        console.wrapped(str(record["traceback"]), 2)


def render_help(console: Console) -> None:
    console.line(console.style("CW by Queopius · Codex Workflow", "1;36"))
    console.line()
    console.line(console.style("Usage", "1"))
    console.line("  cw [command]")
    console.line()
    groups = (
        ("Workflow", (
            ("init", "Initialize CW"),
            ("plan", "Create or inspect development plan"),
            ("start", "Start or resume development"),
            ("status", "Show workflow progress"),
            ("validate", "Validate current phase"),
            ("review", "Run independent review"),
            ("retry", "Retry failed operation"),
            ("history", "View workflow audit trail"),
            ("explain", "Explain workflow integrity or blockers"),
            ("run", "Run a bounded multi-phase batch"),
            ("inspect", "Inspect a managed Codex execution"),
            ("logs", "View structured execution events"),
            ("mcp", "Serve local read-only MCP over stdio"),
        )),
        ("Maintenance", (
            ("doctor", "Check environment"),
            ("repair", "Repair or migrate workflow metadata"),
            ("config", "Show configuration"),
            ("integrations", "Inspect optional and required integrations"),
            ("update", "Check or install CW releases"),
            ("changelog", "Show release history"),
            ("error", "Show detailed last error"),
            ("version", "Show version"),
            ("help", "Show help"),
        )),
    )
    for heading, entries in groups:
        console.line(console.style(heading, "1"))
        for command, description in entries:
            console.line(f"  {command.ljust(14)}{description}")
        console.line()
    console.line(console.style("Examples", "1"))
    for command in ("cw init", "cw plan", "cw", "cw status"):
        console.line(f"  {command}")
    console.line()
    console.line(console.style("Batch execution", "1"))
    console.line("  cw run 3              Run at most 3 gated phases")
    console.line("  cw run --until ID     Run through a target phase")
    console.line("  cw run --resume       Resume an interrupted batch")
    console.line("  --max-time DURATION   Set the wall-clock budget")
    console.line("  --dry-run             Preview without launching Codex")
    console.line()
    console.line(console.style("Observability", "1"))
    console.line("  cw inspect session    Inspect the active/latest run")
    console.line("  cw logs --run ID      View structured execution events")
    console.line("  cw doctor --performance  Show measured startup timings")
    console.line("  cw doctor --processes    Show CW-managed process state")


def render_update_check(console: Console, data: dict[str, Any]) -> None:
    console.header("Update")
    _metric(console, "Installed", data["installed"])
    _metric(console, "Latest", data["latest"])
    _metric(console, "Channel", data["channel"])
    console.line()
    if data["available"]:
        console.line(f"{console.style('↑', '36')} {console.style('Update available', '1;36')}")
        console.run("cw update --info")
        console.action("cw update", "Install the verified release")
    else:
        console.line(console.style(f"{SUCCESS} CW {data['installed']} is up to date", "1;32"))


def render_update_info(console: Console, data: dict[str, Any]) -> None:
    console.header("Update")
    console.line(f"  {console.style(data['installed'], '2')} {console.style('→', '36')} {console.style(data['latest'], '1')}")
    console.line()
    console.section("What's new")
    console.wrapped(data["summary"] or "See the published release notes.", 2)
    if data.get("release_url"):
        console.wrapped(data["release_url"], 2)
    console.line()
    console.section("Compatibility")
    _metric(console, "Project schema", f"{data['minimum_project_schema']}–{data['maximum_project_schema']}", 18)
    _metric(console, "Channel", data["channel"], 18)
    if data["available"]:
        console.run("cw update")


def render_update_result(console: Console, data: dict[str, Any]) -> None:
    console.header("Update")
    if not data.get("installed_now"):
        console.line(console.style(f"{SUCCESS} CW is already up to date", "1;32"))
        return
    _metric(console, "Current", data["previous"] or data["installed"], 14)
    _metric(console, "Available", data["current"], 14)
    _metric(console, "Channel", data["channel"], 14)
    console.line()
    for label in (
        "Release metadata verified", "Package downloaded", "SHA-256 verified",
        "Installation staged", "Smoke test passed",
    ):
        console.line(f"  {console.style(SUCCESS, '32')} {label}")
    console.line()
    console.rule(indent=0)
    console.line()
    console.line(console.style(f"{SUCCESS} CW {data['current']} installed", "1;32"))
    console.line()
    _metric(console, "Previous", data["previous"] or "none", 14)
    _metric(console, "Rollback", "available" if data["rollback_available"] else "unavailable", 14)
    console.line()
    console.action("cw changelog", "View release history")


def render_rollback(console: Console, data: dict[str, Any]) -> None:
    console.header("Rollback")
    _metric(console, "Current", data["previous"], 14)
    _metric(console, "Previous", data["current"], 14)
    console.line()
    console.line(f"  {console.style(ACTIVE, '36')} Restoring previous installation")
    console.line(f"  {console.style(SUCCESS, '32')} Installation verified")
    console.line()
    console.line(console.style(f"CW {data['current']} restored.", "1;32"))


def render_update_notice(console: Console, data: dict[str, Any]) -> None:
    console.line()
    console.rule(indent=0)
    console.line()
    label = f"CW {data['latest']} available"
    console.line(f"{console.style('↑', '36')} {console.style(label, '1')}")
    if data.get("level") == "minor":
        console.wrapped("New workflow capabilities are available.", 2)
    console.action("cw update --info", "Review the release")


def render_changelog(console: Console, releases: list[dict[str, Any]]) -> None:
    console.header("Changelog")
    for release in releases:
        console.line("  " + console.style(str(release["version"]), "1"))
        for item in release.get("changes", []):
            console.wrapped(f"• {item}", 4)
        console.line()


def render_integrations(console: Console, data: dict[str, Any], *, verbose: bool = False, raw: str = "") -> None:
    console.header("Integrations")
    console.section("MCP Servers")
    console.line()
    integrations = data.get("integrations", [])
    if not integrations:
        console.wrapped(f"{PENDING} No MCP servers configured", 2)
    for item in integrations:
        health = item["health"]
        marker = SUCCESS if health == "AVAILABLE" else PENDING if health in {"UNKNOWN", "DISABLED"} else WARNING
        console.line(f"  {console.style(marker, {'✓': '32', '!': '33', '·': '2'}.get(marker, '0'))} {console.style(str(item['id']).title() + ' MCP', '1')}")
        console.field("Status", health.replace("_", " ").title(), 14)
        console.field("Required", "Yes" if item["required"] == "REQUIRED" else "No", 14)
        console.field("Impact", item["impact"].title(), 14)
        if item.get("http_status"):
            console.field("Error", f"HTTP {item['http_status']}", 14)
        if item.get("occurrences", 0) > 1:
            console.field("Suppressed", f"{item['occurrences']} repeated startup diagnostics", 14)
        console.line()
    console.rule()
    console.line()
    if data.get("workflow_can_continue"):
        console.line(console.style(f"{SUCCESS} Workflow can continue", "1;32"))
    else:
        console.line(console.style(f"{ERROR} Required integration unavailable", "1;31"))
    if verbose and raw:
        console.line()
        console.section("Raw diagnostic")
        for line in raw[-6000:].splitlines():
            console.wrapped(line, 2)


def render_batch_preview(console: Console, data: dict[str, Any]) -> None:
    console.header("Batch Preview" if data.get("dry_run") else "Batch Run")
    console.field("Project", data["project"], 14)
    start = next((item for item in data["phases"] if item["id"] == data["start_phase"]), data["phases"][0])
    console.field("Start", f"{start['number']} · {start['name']}", 14)
    console.field("Requested", f"{data['requested_phases']} phases", 14)
    if data.get("large"):
        console.line()
        label = "Large autonomous run" if data.get("strong_warning") else "Extended batch scope"
        console.line(f"  {console.style(WARNING, '33')} {console.style(label, '1;33')}")
        console.wrapped("CW will stop at the first execution or safety limit.", 4)
    console.line()
    console.subsection("Execution budget")
    console.rule()
    console.line()
    _metric(console, "Phases", str(data["requested_phases"]), 16)
    _metric(console, "Time", str(data["max_time"]), 16)
    _metric(console, "Revisions", f"{data['max_revisions']} / phase", 16)
    _metric(console, "Human gates", "STOP", 16)
    console.line()
    console.subsection("Planned scope")
    console.rule()
    console.line()
    for index, phase in enumerate(data["phases"]):
        console.phase(ACTIVE if index == 0 else PENDING, phase["number"], phase["name"], indent=2)
        if phase.get("human_gate"):
            console.wrapped(f"{WARNING} Stops for human approval", 8)
    console.line()
    estimate = data.get("estimate", {})
    console.subsection("Estimated duration")
    if estimate.get("minimum_seconds") is None:
        console.wrapped("Unavailable · insufficient project history", 2)
    else:
        console.wrapped(
            f"~{format_duration(estimate['minimum_seconds'])}–{format_duration(estimate['maximum_seconds'])} · {estimate['confidence']} confidence",
            2,
        )
    console.line()
    console.subsection("Safety")
    for line in (
        "Independent review after every phase", "Valid gate required before advancement",
        "Human review stops execution", "Progress preserved on failure",
    ):
        console.line(f"  {console.style(SUCCESS, '32')} {line}")


def render_batch_outcome(console: Console, outcome: Any, workflow: Any) -> None:
    title = "Batch Complete" if outcome.status == "COMPLETED" else "Batch Stopped"
    console.header(title)
    _metric(console, "Completed", f"{outcome.completed} / {outcome.requested} phases", 16)
    _metric(console, "Duration", format_duration(outcome.elapsed_seconds), 16)
    console.line()
    if outcome.status == "COMPLETED":
        console.line(console.style(f"{SUCCESS} Requested scope completed", "1;32"))
        console.line()
        console.subsection("Reviews")
        _metric(console, "Total", str(getattr(outcome, "reviewer_runs", 0)), 16)
        _metric(console, "Approvals", str(getattr(outcome, "approvals", outcome.completed)), 16)
        _metric(console, "Revisions", str(getattr(outcome, "semantic_revisions", 0)), 16)
        console.line()
        console.subsection("Gates")
        _metric(console, "Verified", str(outcome.completed), 16)
        if outcome.reason == "workflow_complete":
            console.line()
            console.wrapped("Workflow complete. All configured phase gates are valid.", 2)
        elif outcome.current_phase:
            try:
                following = workflow.phase(outcome.current_phase)
                console.line()
                console.field("Next", f"{following.id.split('-', 1)[0]} · {following.name}", 16)
            except KeyError:
                pass
    else:
        marker = WARNING if outcome.status in {"BUDGET_EXHAUSTED", "HUMAN_REVIEW_REQUIRED", "STOPPED"} else ERROR
        console.line(f"{console.style(marker, '33' if marker == WARNING else '31')} {console.style(outcome.reason.replace('_', ' ').title(), '1')}")
        if outcome.current_phase:
            try:
                current = workflow.phase(outcome.current_phase)
                console.field("Current", f"{current.id.split('-', 1)[0]} · {current.name}", 16)
            except KeyError:
                pass
        console.line()
        console.wrapped("Progress was preserved. No unfinished phase was approved.", 2)
        if outcome.status == "STOPPED":
            console.action("cw run --resume", "Resume the remaining original budget")
        elif outcome.status == "BUDGET_EXHAUSTED":
            console.action("cw run", "Start a new bounded execution budget")
        else:
            console.action("cw status", "Inspect the current workflow state")
