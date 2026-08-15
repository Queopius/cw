from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from cw.adapters.codex import CodexAdapter
from cw.core.completion import (
    authorize_extension,
    completion_gate_path,
    contract_payload,
    latest_completion_review,
    load_extension_proposal,
    run_completion_review,
    validate_completion_gate,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.progress import derive_effective_workflow_state, valid_gate_ids
from cw.core.state import save_state
from cw.core.utils import utc_now
from cw.core.workflow import _read_document, load_workflow, write_workflow, workflow_hash
from cw.planning.planner import Planner
from cw.ui.console import Console, emit_json


RootResolver = Callable[[], Path]
ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]


def completion_payload(root: Path, workflow: Any, state: dict[str, Any]) -> dict[str, Any]:
    contract = workflow.completion_target
    effective = derive_effective_workflow_state(root, workflow, state) if workflow.phases else None
    latest = latest_completion_review(root) if contract is not None else None
    proposal = None
    if state.get("extension_proposal"):
        try:
            proposal = load_extension_proposal(root, state, workflow)
        except CwError:
            proposal = None
    results = latest.get("contract_results", []) if isinstance(latest, dict) else []
    verified = sum(item.get("status") == "VERIFIED" for item in results if isinstance(item, dict))
    gate_valid = False
    if contract is not None and completion_gate_path(root).is_file():
        try:
            validate_completion_gate(root, workflow)
            gate_valid = True
        except CwError:
            gate_valid = False
    return {
        "mode": "CONTRACT_AWARE" if contract is not None else "LEGACY",
        "contract": contract_payload(contract) if contract is not None else None,
        "planned_scope_complete": bool(effective and effective.planned_scope_complete),
        "completion_satisfied": gate_valid,
        "state": state.get("status"), "cycle": state.get("completion_cycle", 0),
        "review": latest, "verified_requirements": verified,
        "requirement_count": len(contract.requirements) if contract is not None else 0,
        "proposal": proposal, "authorization_required": proposal is not None,
    }


def _render(console: Console, payload: dict[str, Any]) -> None:
    console.header("Completion")
    if payload["mode"] == "LEGACY":
        console.field("Mode", "LEGACY COMPLETION")
        console.wrapped("This project preserves pre-contract completion semantics.", 2)
        console.action("cw completion adopt --target controlled-pilot", "Opt into a Completion Contract")
        return
    contract = payload["contract"]
    console.field("Target", contract["name"])
    console.field("Planned scope", "COMPLETE" if payload["planned_scope_complete"] else "IN PROGRESS")
    review = payload.get("review")
    console.field("Review", str(review.get("decision")) if isinstance(review, dict) else "PENDING")
    console.field("Satisfied", f"{payload['verified_requirements']} / {payload['requirement_count']} requirements")
    if payload["completion_satisfied"]:
        console.item("✓", "Completion Contract satisfied")
    elif payload.get("proposal"):
        console.line()
        console.subsection("Proposed extension")
        for phase in payload["proposal"]["phases"]:
            console.phase("·", phase["id"].split("-", 1)[0], phase["name"], indent=2)
        console.wrapped("Human authorization is required before CW can continue.", 2)
        console.action("cw completion approve", "Authorize and append these phases")
        console.action("cw completion reject", "Reject without changing phases")
    elif payload["planned_scope_complete"]:
        console.action("cw completion review", "Run independent system-level review")


def _adopt(root: Path, workflow: Any, state: dict[str, Any], target: str | None) -> dict[str, Any]:
    if workflow.completion_target is not None:
        raise CwError("This workflow already has a Completion Contract", ErrorCode.INVALID_STATE)
    if not workflow.phases:
        raise CwError("Create a workflow plan before adopting a Completion Contract", ErrorCode.PLAN_REQUIRED)
    contract = Planner.completion_contract(workflow.goal or "Complete the declared project goal", target_type=target)
    document = _read_document(root / ".codex/workflow/phases.yaml")
    document["completion_target"] = contract
    write_workflow(root / ".codex/workflow/phases.yaml", document)
    updated = load_workflow(root)
    state["workflow_sha256"] = workflow_hash(root / ".codex/workflow/phases.yaml")
    state.update({
        "completion_cycle": 0, "last_completion_review": None,
        "last_completion_gate": None, "extension_proposal": None,
    })
    if len(valid_gate_ids(root, updated)) == len(updated.phases):
        state["status"] = WorkflowState.PLANNED_COMPLETE.value
        state["current_phase"] = None
    state.setdefault("history", []).append({
        "timestamp": utc_now(), "phase": state.get("current_phase"),
        "action": "completion_contract_adopted", "target": contract["id"],
    })
    save_state(root, state)
    return {"adopted": True, "contract": contract, "state": state["status"]}


def command_completion(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    backend_factory: Callable[[], Any] = CodexAdapter,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    action = args.action or "show"
    if action == "show":
        payload = completion_payload(root, workflow, state)
    elif action == "adopt":
        with operation_lock(root, "completion-adopt"):
            payload = _adopt(root, workflow, state, args.target)
    elif action == "review":
        with operation_lock(root, "completion-review"):
            payload = run_completion_review(root, workflow, state, backend_factory())
    elif action in {"approve", "reject"}:
        with operation_lock(root, "completion-authorization"):
            payload = authorize_extension(root, workflow, state, approve=action == "approve")
    else:
        raise CwError("Unknown completion action", ErrorCode.USAGE_ERROR, exit_code=2)
    if args.json:
        emit_json(payload)
    elif action == "show":
        _render(console, payload)
    else:
        console.header("Completion")
        if action == "adopt":
            console.item("✓", f"Completion Contract adopted: {payload['contract']['name']}")
        elif action == "review":
            console.field("Decision", payload["decision"].replace("_", " "))
            console.wrapped(payload["summary"], 2)
            if payload["decision"] == "EXTENSION_REQUIRED":
                console.wrapped("Human authorization is required before CW can continue.", 2)
                console.action("cw completion show", "Inspect the proposed extension")
        elif action == "approve":
            console.item("✓", "Extension authorized")
            console.field("Current phase", payload["current_phase"])
            console.action("cw", "Start the first appended phase")
        else:
            console.item("✓", "Extension rejected; no phases were changed")
    if action == "review" and payload.get("decision") in {"EXTENSION_REQUIRED", "BLOCKED"}:
        return 3
    return 0
