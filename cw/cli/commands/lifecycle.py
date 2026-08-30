from __future__ import annotations

import argparse
import copy
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cw.adapters.codex import CodexAdapter
from cw.core.authorization import (
    Actor,
    ActorOrigin,
    OperationContext,
    issue_user_authorization,
)
from cw.core.diagnostics import state_error
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path, validate_gate
from cw.core.initialize import backup_metadata, initialize
from cw.core.initialize import repair as repair_metadata
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.plan_amendment import (
    amend_plan,
    apply_active_artifact_amendment,
    prepare_active_artifact_amendment,
)
from cw.core.rebaseline_recovery import (
    apply_rebaseline_recovery,
    preview_rebaseline_recovery,
    recover_rebaseline_recovery_transaction,
    write_reopen_receipt,
)
from cw.core.recovery import mark_infrastructure_error
from cw.core.revisions import (
    apply_rebaseline,
    authorization_resource,
    create_rebaseline_proposal,
    load_proposal,
    persist_revision,
    revision_payload,
)
from cw.core.state import bind_plan, initial_state, save_state, transition
from cw.core.utils import safe_project_path, utc_now
from cw.core.workflow import (
    _read_document,
    load_workflow,
    set_plan_status,
    workflow_hash,
    write_workflow,
)
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
    payload = {
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
    if workflow.completion_target is not None:
        from cw.core.completion import contract_payload

        payload["completion_target"] = contract_payload(workflow.completion_target)
    else:
        payload["completion_target"] = None
    return payload


def _show_plan(
    args: argparse.Namespace,
    console: Console,
    root: Path,
    state: dict[str, Any],
    workflow: Any,
) -> int:
    payload = _plan_payload(workflow)
    payload["workflow_sha256"] = workflow_hash(root / ".codex/workflow/phases.yaml")
    from cw.core.utils import sha256_file

    payload["state_sha256"] = sha256_file(root / ".cw/state.json")
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
        if workflow.completion_target is not None:
            console.field("Completion target", workflow.completion_target.name)
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
        document = _read_document(root / ".codex/workflow/phases.yaml")
        revision = revision_payload(
            root, document, parent_revision_id=None,
            actor_id="local-operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
        )
        persist_revision(root, revision)
        previous_revision = state.get("active_plan_revision")
        if isinstance(previous_revision, str) and previous_revision != revision["plan_revision_id"]:
            state["superseded_plan_revisions"] = [
                *[item for item in state.get("superseded_plan_revisions", []) if isinstance(item, str)],
                previous_revision,
            ]
        state["workflow_sha256"] = workflow_hash(root / ".codex" / "workflow" / "phases.yaml")
        state["active_plan_revision"] = revision["plan_revision_id"]
        state["active_plan_revision_sha256"] = revision["canonical_workflow_sha256"]
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
    if args.action == "rebaseline" and getattr(args, "rebaseline_action", None) == "recover":
        recovery_required = (
            args.phase, args.review_ref, args.expected_review_sha256,
            args.expected_workflow_sha256, args.expected_state_sha256, args.reason,
        )
        prior_gate_complete = (
            (bool(args.no_prior_gate) and not (args.expected_prior_gate_ref or args.expected_prior_gate_sha256))
            or (
                not args.no_prior_gate
                and bool(args.expected_prior_gate_ref and args.expected_prior_gate_sha256)
            )
        )
        if not all(recovery_required) or not prior_gate_complete or bool(args.dry_run) == bool(args.apply is True):
            raise CwError(
                "Rebaseline recovery requires phase, review reference, three CAS values, prior-gate authority, reason, and exactly one of --dry-run or --apply",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
        if isinstance(args.apply, str):
            raise CwError("Rebaseline recovery --apply does not accept a value", ErrorCode.USAGE_ERROR, exit_code=2)
        if args.goal or args.proposal or args.authorize or args.add_artifact or args.file:
            raise CwError("Rebaseline recovery received options for another operation", ErrorCode.USAGE_ERROR, exit_code=2)
        if args.dry_run:
            payload = preview_rebaseline_recovery(
                root, args.phase, args.review_ref, args.expected_review_sha256,
                args.expected_workflow_sha256, args.expected_state_sha256, args.reason,
                expected_prior_gate_reference=None if args.no_prior_gate else args.expected_prior_gate_ref,
                expected_prior_gate_sha256=None if args.no_prior_gate else args.expected_prior_gate_sha256,
            )
        else:
            with operation_lock(root, "plan-rebaseline-recover"):
                recover_rebaseline_recovery_transaction(root)
                payload = apply_rebaseline_recovery(
                    root, args.phase, args.review_ref, args.expected_review_sha256,
                    args.expected_workflow_sha256, args.expected_state_sha256, args.reason,
                    expected_prior_gate_reference=None if args.no_prior_gate else args.expected_prior_gate_ref,
                    expected_prior_gate_sha256=None if args.no_prior_gate else args.expected_prior_gate_sha256,
                )
        if args.json:
            emit_json(payload)
        else:
            console.header("Plan rebaseline recovery")
            console.field("Status", payload["status"])
            console.field("Phase", payload["phase"])
            console.field("Review", payload["review_reference"])
            console.field("Review SHA-256", payload["review_sha256"])
            console.field("Workflow CAS", payload["workflow_sha256"])
            console.field("State CAS", payload["state_sha256"])
            console.field("Previous state", payload["previous_status"])
            console.field("Resulting state", payload["resulting_status"])
            if payload.get("idempotent_replay") is True:
                console.item("↻", "Recovery already applied — idempotent replay; no project changes were made.")
                console.field("Recovery ID", payload.get("recovery_id"))
            if payload.get("backup"):
                console.field("Backup", payload["backup"])
            if payload.get("recovery_receipt"):
                console.field("Recovery receipt", payload["recovery_receipt"])
            console.action(
                "cw plan rebaseline --proposal <file> --reason <reason>",
                "Create a separate proposal requiring independent apply authorization",
            )
        return 0
    if getattr(args, "rebaseline_action", None) is not None:
        raise CwError("Recovery requires 'cw plan rebaseline recover'", ErrorCode.USAGE_ERROR, exit_code=2)
    recovery_options = (
        getattr(args, "review_ref", None),
        getattr(args, "expected_review_sha256", None),
        getattr(args, "expected_prior_gate_ref", None),
        getattr(args, "expected_prior_gate_sha256", None),
        getattr(args, "no_prior_gate", False),
    )
    if any(value not in {None, False} for value in recovery_options):
        raise CwError(
            "Review recovery options require 'cw plan rebaseline recover'",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    amend_options = (
        getattr(args, "file", None),
        getattr(args, "expected_workflow_sha256", None),
        getattr(args, "expected_state_sha256", None),
        getattr(args, "phase", None),
        getattr(args, "add_artifact", None),
        getattr(args, "dry_run", False),
        getattr(args, "yes", False),
        getattr(args, "non_interactive", False),
    )
    rebaseline_options = tuple(
        getattr(args, name, None)
        for name in ("proposal", "authorize", "operation_id")
    )
    if args.action != "rebaseline" and any(value not in {None, False} for value in rebaseline_options):
        raise CwError(
            "Rebaseline options require 'cw plan rebaseline'",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    if args.action != "amend" and any(value not in {None, False} for value in amend_options):
        raise CwError(
            "Amendment options require 'cw plan amend'",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    if args.action not in {"amend", "rebaseline"} and any(
        value not in {None, False}
        for value in (getattr(args, "reason", None), getattr(args, "apply", None))
    ):
        raise CwError(
            "Reason and apply options require plan amend or plan rebaseline",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    if args.action == "amend":
        if any(value not in {None, False} for value in rebaseline_options) or args.goal:
            raise CwError(
                "Plan amend received options for another plan action",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
        active_mode = bool(args.phase or args.add_artifact or args.expected_state_sha256 or args.reason or args.dry_run or args.apply is True or args.yes or args.non_interactive)
        file_mode = bool(args.file)
        if file_mode and active_mode:
            raise CwError(
                "Plan amend --file and active artifact options are mutually exclusive",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
        if file_mode:
            if not args.expected_workflow_sha256:
                raise CwError(
                    "Plan amend --file requires --expected-workflow-sha256",
                    ErrorCode.USAGE_ERROR,
                    exit_code=2,
                )
            with operation_lock(root, "plan-amend"):
                payload = amend_plan(root, args.file, args.expected_workflow_sha256)
        else:
            if isinstance(args.apply, str):
                raise CwError("Active plan amend --apply does not accept a value", ErrorCode.USAGE_ERROR, exit_code=2)
            required = (
                args.phase, args.add_artifact, args.expected_workflow_sha256,
                args.expected_state_sha256, args.reason,
            )
            if not all(required) or bool(args.dry_run) == bool(args.apply is True):
                raise CwError(
                    "Active plan amend requires phase, artifacts, both hashes, reason, and exactly one of --dry-run or --apply",
                    ErrorCode.USAGE_ERROR,
                    exit_code=2,
                )
            if args.dry_run:
                payload = prepare_active_artifact_amendment(
                    root, args.phase, args.add_artifact,
                    args.expected_workflow_sha256, args.expected_state_sha256, args.reason,
                )
            else:
                if args.non_interactive and not args.yes:
                    raise CwError(
                        "Non-interactive amendment requires --yes",
                        ErrorCode.AUTHORIZATION_REQUIRED,
                        exit_code=3,
                    )
                if not args.yes and input("\nApply this exact artifact-only plan amendment? [y/N] ").strip().lower() not in {"y", "yes"}:
                    raise CwError("Plan amendment was not confirmed", ErrorCode.AUTHORIZATION_REQUIRED, exit_code=3)
                payload = apply_active_artifact_amendment(
                    root, args.phase, args.add_artifact,
                    args.expected_workflow_sha256, args.expected_state_sha256, args.reason,
                )
        if args.json:
            emit_json(payload)
        else:
            console.header("Plan amendment" if payload.get("dry_run") else "Plan amended")
            console.item("✓", "Validated without writes" if payload.get("dry_run") else "Plan changed safely")
            console.field("State", payload["status"])
            if "phase_count" in payload:
                console.field("Phases", payload["phase_count"])
            if "phase" in payload:
                console.field("Phase", payload["phase"])
                console.field("Added artifacts", ", ".join(payload["added_artifacts"]))
                console.field("Semantic removals", len(payload["removed_artifacts"]))
                console.field("Other semantic changes", len(payload["other_changes"]))
            if payload.get("backup"):
                console.field("Backup", payload["backup"])
            console.field("Previous workflow", payload.get("previous_workflow_sha256", payload.get("expected_workflow_sha256")))
            console.field("Current workflow", payload["workflow_sha256"])
            if payload.get("previous_state_sha256") or payload.get("expected_state_sha256"):
                console.field("Expected state", payload.get("previous_state_sha256", payload.get("expected_state_sha256")))
            console.field("Completion Contract", "preserved")
            console.field("Approval required", "YES")
            if not payload.get("dry_run"):
                console.run("cw plan approve")
        return 0
    project, state, workflow = context(root)
    if args.action == "show":
        return _show_plan(args, console, root, state, workflow)
    if args.action == "approve":
        return _approve_plan(args, console, root, state, workflow)
    if args.action == "rebaseline":
        actor = Actor("local-operator", ActorOrigin.HUMAN_CLI, explicit_user_intent=True)
        if args.apply:
            if args.apply is True:
                raise CwError("Rebaseline --apply requires a proposal ID", ErrorCode.USAGE_ERROR, exit_code=2)
            if args.goal or args.proposal or args.reason:
                raise CwError(
                    "Rebaseline apply accepts only --apply, --authorize, and --operation-id",
                    ErrorCode.USAGE_ERROR,
                    exit_code=2,
                )
            if not args.authorize:
                raise CwError(
                    "Plan rebaseline requires explicit --authorize",
                    ErrorCode.AUTHORIZATION_REQUIRED,
                    "Inspect the proposal, then repeat with --authorize.",
                    exit_code=3,
                )
            proposal = load_proposal(root, args.apply)
            operation_id = args.operation_id or uuid.uuid4().hex
            grant = issue_user_authorization(
                action="plan.rebaseline",
                resource_id=authorization_resource(proposal),
                operation_id=operation_id,
                actor=actor,
            )
            with operation_lock(root, "plan-rebaseline"):
                payload = apply_rebaseline(
                    root, workflow, state, args.apply,
                    OperationContext(operation_id, actor, "plan.rebaseline", grant),
                )
        else:
            if args.authorize or args.operation_id:
                raise CwError(
                    "Rebaseline preview cannot authorize or select an operation ID",
                    ErrorCode.USAGE_ERROR,
                    exit_code=2,
                )
            if not args.reason or not args.reason.strip():
                raise CwError("Plan rebaseline requires --reason", ErrorCode.USAGE_ERROR, exit_code=2)
            if bool(args.goal) == bool(args.proposal):
                raise CwError(
                    "Plan rebaseline preview requires exactly one of --goal or --proposal",
                    ErrorCode.USAGE_ERROR,
                    exit_code=2,
                )
            if args.proposal:
                proposal_file = safe_project_path(root, args.proposal, must_exist=True)
                if not proposal_file.is_file() or proposal_file.is_symlink():
                    raise CwError("Rebaseline proposal path is unsafe", ErrorCode.USAGE_ERROR, exit_code=2)
                proposed_document = _read_document(proposal_file)
            else:
                planner = Planner(
                    workflow.human_gate_categories,
                    backend=CodexAdapter(),
                    timeout=workflow.review_timeout,
                )
                proposed_document = planner.propose_plan(root, project.project_id, args.goal)
            with operation_lock(root, "plan-rebaseline-proposal"):
                payload = create_rebaseline_proposal(
                    root, workflow, state, proposed_document,
                    reason=args.reason, actor_id=actor.actor_id,
                    actor_origin=actor.origin.value,
                )
            payload = {
                "status": "PROPOSED",
                "proposal_id": payload["proposal_id"],
                "proposal_sha256": payload["proposal_sha256"],
                "old_plan_revision_id": payload["old_plan_revision_id"],
                "new_plan_revision_id": payload["new_plan_revision_id"],
                "phase": payload["phase"],
                "reason": payload["reason"],
                "authorization_required": True,
                "apply_command": f"cw plan rebaseline --apply {payload['proposal_id']} --authorize",
            }
        if args.json:
            emit_json(payload)
        else:
            console.header("Plan rebaseline")
            console.field("Status", payload["status"])
            console.field("Proposal", payload.get("proposal_id", args.apply))
            console.field("Old revision", payload.get("old_plan_revision_id"))
            console.field("New revision", payload.get("new_plan_revision_id"))
            if payload.get("authorization_required"):
                console.wrapped("Human authorization is required for this exact proposal hash.", 2)
                console.action(payload["apply_command"], "Authorize and activate the corrected plan")
        return 0
    with operation_lock(root, "plan"):
        if args.action == "rebuild":
            if state.get("status") == WorkflowState.REVISION_REQUIRED.value or any((root / ".cw/reviews").glob("*.json")):
                raise CwError(
                    "Reviewed workflows must use an auditable plan rebaseline",
                    ErrorCode.PLAN_REBASELINE_REQUIRED,
                    'Run: cw plan rebaseline --goal "..." --reason "..."',
                )
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
        except KeyboardInterrupt:
            interrupted = CwError(
                "Planning was interrupted before a plan was persisted",
                ErrorCode.PLANNER_TRANSPORT_ERROR,
                "Run: cw retry",
                details="stage=planner_process provider=codex mode=stdin retry_safe=true interrupted=true",
            )
            state["last_error"] = state_error(interrupted)
            mark_infrastructure_error(
                state, interrupted, operation="planning", phase=None,
            )
            state.setdefault("history", []).append({
                "timestamp": utc_now(), "phase": None,
                "action": "planning_failed", "operation": "planning",
                "error_code": interrupted.code.value,
            })
            transition(root, state, WorkflowState.ERROR, force_error=True)
            raise
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
                state.setdefault("history", []).append({
                    "timestamp": utc_now(), "phase": None,
                    "action": "planning_failed", "operation": "planning",
                    "error_code": exc.code.value,
                })
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
    output = {
        "status": "PROPOSED", "goal": workflow.goal, "phases": len(workflow.phases),
        "completion_target": workflow.completion_target.name if workflow.completion_target else None,
    }
    if args.json:
        emit_json(output)
    else:
        console.header("Plan")
        console.item("✓", "Plan proposed")
        console.field("Goal", workflow.goal)
        console.field("Phases", len(workflow.phases))
        if workflow.completion_target is not None:
            console.field("Completion target", workflow.completion_target.name)
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
    repair_report: dict[str, Any] = {}
    with operation_lock(root, "repair"):
        if args.reopen:
            _, state, workflow = context(root)
            before_state = copy.deepcopy(state)
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
                "revision_attempt": 0,
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
            receipt = write_reopen_receipt(
                root, phase_id=target.id, before_state=before_state,
                after_state=state, backup=backup,
            )
            if receipt is not None:
                state["history"][-1]["receipt"] = receipt[0]
                state["history"][-1]["receipt_sha256"] = receipt[1]
                save_state(root, state)
            reopened = target.id
        else:
            backup = repair_metadata(root, report=repair_report)
    payload = {
        "repaired": reopened is None,
        "reopened": reopened,
        "backup": backup.relative_to(root).as_posix(),
        "legacy_completion_evidence": repair_report.get("legacy_completion_evidence", []),
    }
    if args.json:
        emit_json(payload)
    else:
        console.header("Repair")
        if reopened:
            console.item("✓", f"Phase reopened: {reopened}")
            console.field("Backup", payload["backup"])
        else:
            console.item("✓", "Backup created")
            for migration in repair_report.get("legacy_completion_evidence", []):
                console.item("✓", f"Archived {migration['source']}")
                console.field("Preserved", str(migration["preserved_at"]))
            verified = repair_report.get("gates_verified", [])
            if verified:
                console.item("✓", f"{len(verified)} approval gates verified")
            console.item("✓", "State reconciled" if repair_report.get("state_reconciled") else "State already consistent")
            reconstructed = int(repair_report.get("history_reconstructed", 0))
            if reconstructed:
                console.item("✓", f"History reconstructed · {reconstructed} event{'s' if reconstructed != 1 else ''}")
            else:
                console.item("✓", "History already complete")
            console.item("✓", "Integrity baseline refreshed")
            console.item("✓", "Existing approvals preserved")
            after = repair_report.get("state_after")
            if isinstance(after, dict) and after.get("status") == WorkflowState.COMPLETED.value:
                console.line()
                console.section("Workflow")
                console.item("✓", "COMPLETE")
                console.field(
                    "Approved",
                    f"{repair_report.get('approved_count', 0)} / {repair_report.get('phase_count', 0)} phases",
                )
            elif isinstance(after, dict) and after.get("current_phase"):
                console.line()
                console.section("Current")
                console.item("→", str(after["current_phase"]))
            if not repair_report.get("state_reconciled") and reconstructed == 0:
                console.line()
                console.wrapped("No repairs required.", 2)
            console.line()
            console.wrapped("No application code was changed.", 2)
            console.field("Backup", payload["backup"])
    return 0
