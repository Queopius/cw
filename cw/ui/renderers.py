from __future__ import annotations

from typing import Any

from .console import Console, error_summary
from .symbols import ACTIVE, ERROR, PENDING, SUCCESS, WARNING


def render_status(console: Console, data: dict[str, Any], *, verbose: bool = False) -> None:
    console.header()
    console.line(f"  {console.style(data['project'], '1')}  ·  {data['branch']}")
    console.line()
    console.field("Workflow", data["workflow"])
    console.field("Plan", data["plan"])
    console.field("State", data["state"])
    if data["phase"]:
        current = data["phases"][data["phase_index"]]
        console.line()
        console.field("Phase", f"{current['number']} · {current['name']}")
        console.field("Position", f"{data['position']} / {data['phase_count']}")
        console.field("Approved", f"{data['approved_count']} / {data['phase_count']}")
        console.field("Attempt", f"{data['attempt']} / {data['max_attempts']}")
        console.line()
        for phase in data["phases"]:
            gate_state = data["gate_states"].get(phase["id"], "pending")
            marker = WARNING if gate_state == "invalid" else SUCCESS if gate_state == "approved" else ACTIVE if phase["id"] == data["phase"] else PENDING
            console.phase(marker, phase["number"], phase["name"])
        console.line()
        console.field("Readiness", "READY" if data["ready"] else "NOT READY")
        console.field("Gate", "APPROVED" if data["gate"] else "PENDING")
    else:
        console.line()
        console.field("Plan", "NOT CREATED")
        console.run("cw plan")

    if data.get("gate_error"):
        console.line()
        console.item(WARNING, "Approval gate invalidated")
        console.run("cw error")
    recovery = data.get("infrastructure_error")
    if data["state"] == "ERROR" and isinstance(recovery, dict) and recovery.get("retryable") is True:
        console.line()
        code = str(recovery.get("error_code", ""))
        title, detail = error_summary(code, str(data.get("last_error") or ""))
        console.item(ERROR, title)
        console.wrapped(detail)
        console.run("cw retry")
    elif data["state"] == "ERROR" and data.get("last_error"):
        code = data["last_error"].split(":", 1)[0]
        title, detail = error_summary(code, data["last_error"])
        console.line()
        console.item(ERROR, title)
        console.wrapped(detail)
        console.run("cw error")
    if verbose:
        console.line()
        console.section("Details")
        console.field("Root", data["repository_root"])
        console.field("State file", ".cw/state.json")
        if data.get("invalid_gates"):
            console.field("Invalid gates", ", ".join(data["invalid_gates"]))


def render_history(console: Console, phases: list[dict[str, Any]], *, verbose: bool = False) -> None:
    console.header("History")
    if not phases:
        console.item(PENDING, "No workflow evidence recorded")
        return
    labels = {
        "approved": "Approved",
        "revision_required": "Revision required",
        "infrastructure_failure": "Infrastructure failure",
        "infrastructure_failure_recovered": "Recovered infrastructure failure",
        "human_review_required": "Human review required",
        "current": "Current",
    }
    for phase in phases:
        marker = SUCCESS if phase["approved"] else ACTIVE if phase["current"] else WARNING
        console.phase(marker, phase["number"], phase["name"], indent=0)
        for entry in phase["entries"]:
            label = labels[entry["kind"]]
            attempt = entry.get("attempt")
            summary = f"{label} · attempt {attempt}" if attempt else label
            console.line(f"  {summary}")
            if verbose:
                if entry.get("timestamp"):
                    console.field("When", entry["timestamp"], 8)
                if entry.get("review"):
                    console.field("Review", entry["review"], 8)
                if entry.get("gate"):
                    console.field("Gate", entry["gate"], 8)
                if entry.get("error_code"):
                    console.field("Error", entry["error_code"], 8)
        console.line()


def render_doctor(console: Console, checks: list[dict[str, Any]], result: dict[str, int], *, verbose: bool = False) -> None:
    console.header("Doctor")
    sections = list(dict.fromkeys(check["section"] for check in checks))
    ordered = [section for section in ("Environment", "Workflow", "Security") if section in sections]
    ordered.extend(section for section in sections if section not in ordered)
    for section_index, section in enumerate(ordered):
        if section_index:
            console.line()
        console.section(section)
        for check in (item for item in checks if item["section"] == section):
            marker = {"pass": SUCCESS, "warning": WARNING, "error": ERROR, "neutral": PENDING}[check["status"]]
            console.item(marker, check["name"])
            if verbose or check["status"] in {"warning", "error", "neutral"} or check["name"] == "Reviewer connectivity":
                console.wrapped(str(check["detail"]), 2)
    console.line()
    console.section("Result")
    console.item(SUCCESS, f"{result['passed']} checks passed")
    console.item(PENDING, f"{result['warnings']} warnings")
    console.item(ERROR, f"{result['errors']} errors")
