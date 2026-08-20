from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable

from cw.core.errors import CwError, ErrorCode
from cw.core.models import WorkflowState
from cw.core.progress import derive_effective_workflow_state
from cw.core.platform import process_is_alive
from cw.execution.batch import BatchOutcome, BatchRunner
from cw.execution.budget import ExecutionBudget
from cw.execution.config import load_execution_settings
from cw.execution.duration import format_duration, parse_duration
from cw.execution.estimator import ExecutionEstimator
from cw.execution.session import batch_lock, completed_phase_durations, load_batch, save_batch
from cw.ui.console import Console, emit_json
from cw.ui.renderers import render_batch_outcome, render_batch_preview, render_completed_action


RootResolver = Callable[[], Path]
ContextLoader = Callable[[Path], tuple[Any, dict[str, Any], Any]]
Executor = Callable[[str, float], int]


def command_run(
    args: argparse.Namespace,
    console: Console,
    *,
    root_resolver: RootResolver,
    context: ContextLoader,
    executor: Executor,
    runner_factory: Callable[[], BatchRunner] = BatchRunner,
) -> int:
    root = root_resolver()
    _, state, workflow = context(root)
    if not workflow.phases:
        raise CwError("Development plan required", ErrorCode.PLAN_REQUIRED, "Run: cw plan", exit_code=3)
    effective_state = derive_effective_workflow_state(root, workflow, state)
    if effective_state.is_complete:
        payload = {
            "status": "COMPLETED",
            "approved": effective_state.approved_count,
            "phases": len(workflow.phases),
            "available_phases": 0,
            "implementation_started": False,
        }
        if args.json:
            emit_json(payload)
        else:
            render_completed_action(
                console,
                workflow,
                title="Batch Run",
                detail="0 phases are available to run. No implementation session was started.",
            )
        return 0
    if effective_state.planned_scope_complete:
        payload = {
            "status": "PLANNED_COMPLETE", "approved": effective_state.approved_count,
            "phases": len(workflow.phases), "available_phases": 0,
            "implementation_started": False, "next": "cw completion review",
        }
        if args.json:
            emit_json(payload)
        else:
            console.header("Batch Run")
            console.item("✓", "All authorized phase gates are valid")
            console.wrapped("Completion review is required before semantic completion.", 2)
            console.run("cw completion review")
        return 3
    settings = load_execution_settings(root)
    existing = load_batch(root)
    if (
        existing
        and existing.get("status") == "RUNNING"
        and (not isinstance(existing.get("pid"), int) or not _alive(existing["pid"]))
    ):
        existing = {**existing, "status": "INTERRUPTED", "pid": None}
        save_batch(root, existing)
    if args.resume:
        if args.phase_count is not None or args.phases is not None or args.until or args.max_time:
            raise CwError("--resume cannot be combined with a new budget", ErrorCode.USAGE_ERROR, exit_code=2)
        resumable = {"INTERRUPTED", "STOPPED"}
        if not existing or existing.get("status") not in resumable:
            raise CwError("No safely resumable batch is available", ErrorCode.INVALID_STATE, exit_code=3)
        requested = int(existing["requested_phases"])
        max_time = int(existing["max_time_seconds"])
        session = existing
    else:
        if args.until and (args.phase_count is not None or args.phases is not None):
            raise CwError("--until cannot be combined with a phase count", ErrorCode.USAGE_ERROR, exit_code=2)
        if args.phase_count is not None and args.phases is not None and args.phase_count != args.phases:
            raise CwError("Positional phase count and --phases conflict", ErrorCode.USAGE_ERROR, exit_code=2)
        requested = args.phases if args.phases is not None else args.phase_count
        requested = settings.default_phases if requested is None else requested
        max_time = parse_duration(args.max_time) if args.max_time else settings.default_max_time_seconds
        session = None
    if requested < 1:
        raise CwError("Batch phase count must be positive", ErrorCode.USAGE_ERROR, exit_code=2)
    current_id = state.get("current_phase")
    if not isinstance(current_id, str):
        raise CwError("No current phase is available", ErrorCode.INVALID_STATE)
    current_index = workflow.index(current_id)
    if args.until:
        try:
            target_index = workflow.index(args.until)
        except (StopIteration, ValueError) as exc:
            raise CwError(f"Unknown target phase: {args.until}", ErrorCode.USAGE_ERROR, exit_code=2) from exc
        if target_index < current_index:
            raise CwError("Batch target is before the current phase", ErrorCode.USAGE_ERROR, exit_code=2)
        requested = target_index - current_index + 1
    if requested > settings.hard_max_phases:
        raise CwError(
            f"Batch requests {requested} phases; maximum is {settings.hard_max_phases}",
            ErrorCode.BATCH_TOO_LARGE,
            f"cw run --phases {settings.hard_max_phases}",
            details=f"Requested    {requested} phases\nMaximum      {settings.hard_max_phases} phases",
            exit_code=2,
        )
    remaining_workflow = len(workflow.phases) - current_index
    effective = min(requested, remaining_workflow)
    phases = list(workflow.phases[current_index:current_index + effective])
    estimate = ExecutionEstimator().estimate(
        state.get("history", []),
        effective,
        completed_durations=completed_phase_durations(root),
    )
    preview = {
        "project": workflow.id, "start_phase": current_id, "requested_phases": requested,
        "effective_phases": effective, "max_time_seconds": max_time,
        "max_time": format_duration(max_time),
        "max_revisions": min(settings.max_semantic_revisions_per_phase, workflow.max_review_attempts),
        "phases": [
            {"id": phase.id, "number": phase.id.split("-", 1)[0], "name": phase.name,
             "human_gate": phase.requires_human_approval}
            for phase in phases
        ],
        "large": requested > settings.recommended_max_phases,
        "strong_warning": requested >= 6,
        "estimate": {
            "minimum_seconds": estimate.minimum_seconds,
            "maximum_seconds": estimate.maximum_seconds,
            "confidence": estimate.confidence, "basis": estimate.basis,
        },
        "dry_run": args.dry_run,
        "workflow_state": state["status"],
    }
    if args.dry_run:
        if args.json:
            emit_json(preview)
        else:
            render_batch_preview(console, preview)
            console.line()
            console.wrapped("No work was started.", 2)
        return 0
    if args.json:
        raise CwError("JSON mode is supported for batch dry-run only", ErrorCode.USAGE_ERROR, "Run: cw run --dry-run --json", exit_code=2)
    allowed_states = {
        WorkflowState.READY.value, WorkflowState.IN_PROGRESS.value,
        WorkflowState.REVISION_REQUIRED.value, WorkflowState.READY_FOR_REVIEW.value,
    }
    if state["status"] not in allowed_states:
        raise CwError(f"Cannot start batch while workflow is {state['status']}", ErrorCode.INVALID_STATE, "Run: cw status")
    render_batch_preview(console, preview)
    if preview["strong_warning"] and not args.yes:
        if args.non_interactive or not sys.stdin.isatty():
            raise CwError("Large batch requires explicit confirmation", ErrorCode.WORKFLOW_ERROR, "Run again with --yes", exit_code=3)
        answer = input(f"\n  Continue with {requested} phases? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise CwError("Batch cancelled", ErrorCode.WORKFLOW_ERROR, exit_code=3)
    budget = ExecutionBudget(
        max_phases=requested, max_wall_time_seconds=max_time,
        max_semantic_revisions_per_phase=min(settings.max_semantic_revisions_per_phase, workflow.max_review_attempts),
    )
    with batch_lock(root):
        outcome = runner_factory().run(root, workflow, budget, executor, session=session)
    render_batch_outcome(console, outcome, workflow)
    return outcome.exit_code


def _alive(pid: int) -> bool:
    return process_is_alive(pid)
