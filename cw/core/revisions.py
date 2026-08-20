from __future__ import annotations

import copy
import json
import re
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cw import __version__

from .authorization import OperationContext, validate_authorization
from .errors import CwError, ErrorCode
from .layout import safe_directory, safe_file
from .models import ReviewDecision, Workflow, WorkflowState
from .reviews import validate_reviewer_result
from .schema import SCHEMA_VERSION, schema_version
from .utils import (
    atomic_json,
    atomic_json_new,
    load_json,
    safe_project_path,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .workflow import _read_document, workflow_from_document, workflow_hash, write_workflow


REVISION_ID = re.compile(r"pr-[0-9a-f]{64}")
PROPOSAL_ID = re.compile(r"pp-[0-9a-f]{64}")
SUPERSESSION_ID = re.compile(r"ps-[0-9a-f]{64}")
TRANSACTION = ".cw/runtime/plan-rebaseline-transaction.json"


def _gate_path(root: Path, phase_id: str) -> Path:
    return root / ".cw" / "gates" / f"{phase_id}.approved.json"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_document_hash(document: dict[str, Any]) -> str:
    return sha256_bytes(_canonical_bytes(document))


def revision_id(document: dict[str, Any]) -> str:
    return "pr-" + canonical_document_hash(document).removeprefix("sha256:")


def proposal_id(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key not in {"proposal_id", "proposal_sha256"}}
    return "pp-" + sha256_bytes(_canonical_bytes(body)).removeprefix("sha256:")


def _git_candidate(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False, stdin=subprocess.DEVNULL,
        timeout=10,
    )
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) else None


def artifact_revision_metadata(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
    *,
    include_legacy: bool = False,
) -> dict[str, Any]:
    identifier, canonical = active_revision(root, state, workflow)
    if state.get("active_plan_revision") is None and not include_legacy:
        return {}
    return {
        "plan_revision_id": identifier,
        "canonical_workflow_sha256": canonical,
        "candidate_sha": _git_candidate(root),
        "cw_version": __version__,
        "workflow_schema_version": SCHEMA_VERSION,
    }


def revision_path(root: Path, identifier: str) -> Path:
    if REVISION_ID.fullmatch(identifier) is None:
        raise CwError("Plan revision identity is invalid", ErrorCode.PLAN_REVISION_INVALID)
    return root / ".cw" / "plan-revisions" / f"{identifier}.json"


def proposal_path(root: Path, identifier: str) -> Path:
    if PROPOSAL_ID.fullmatch(identifier) is None:
        raise CwError("Plan proposal identity is invalid", ErrorCode.PLAN_REVISION_INVALID)
    return root / ".cw" / "plan-proposals" / f"{identifier}.json"


def supersession_path(root: Path, identifier: str) -> Path:
    if SUPERSESSION_ID.fullmatch(identifier) is None:
        raise CwError("Plan supersession identity is invalid", ErrorCode.SUPERSESSION_INVALID)
    return root / ".cw" / "supersessions" / f"{identifier}.json"


def active_revision(root: Path, state: dict[str, Any], workflow: Workflow | None = None) -> tuple[str, str]:
    identifier = state.get("active_plan_revision")
    canonical = state.get("active_plan_revision_sha256")
    if identifier is not None or canonical is not None:
        if not isinstance(identifier, str) or REVISION_ID.fullmatch(identifier) is None or not isinstance(canonical, str):
            raise CwError("Active plan revision metadata is invalid", ErrorCode.PLAN_REVISION_INVALID)
        snapshot = load_revision(root, identifier)
        if snapshot["canonical_workflow_sha256"] != canonical:
            raise CwError("Active plan revision hash is inconsistent", ErrorCode.PLAN_REVISION_INVALID)
        return identifier, canonical
    document = _read_document(root / ".codex/workflow/phases.yaml")
    return revision_id(document), canonical_document_hash(document)


def revision_payload(
    root: Path,
    document: dict[str, Any],
    *,
    parent_revision_id: str | None,
    actor_id: str,
    actor_origin: str,
    authorization_reference: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    workflow = workflow_from_document(root, document)
    identifier = revision_id(document)
    contract = document.get("completion_target")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "plan_revision",
        "plan_revision_id": identifier,
        "canonical_workflow_sha256": canonical_document_hash(document),
        "source_workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml") if (
            _read_document(root / ".codex/workflow/phases.yaml") == document
        ) else None,
        "parent_plan_revision_id": parent_revision_id,
        "created_at": created_at or utc_now(),
        "cw_version": __version__,
        "workflow_schema_version": int(document.get("schema_version", SCHEMA_VERSION)),
        "workflow_id": workflow.id,
        "goal": workflow.goal,
        "completion_contract_sha256": sha256_bytes(_canonical_bytes(contract)) if isinstance(contract, dict) else None,
        "actor": {"actor_id": actor_id, "origin": actor_origin},
        "authorization_reference": authorization_reference,
        "workflow": copy.deepcopy(document),
    }


def persist_revision(root: Path, payload: dict[str, Any]) -> tuple[Path, bool]:
    validated = validate_revision_payload(root, payload)
    directory = safe_directory(root / ".cw" / "plan-revisions", ".cw/plan-revisions", create=True)
    path = directory / f"{validated['plan_revision_id']}.json"
    if path.exists():
        existing = load_json(safe_file(path, "Plan revision", required=True))
        if existing != validated:
            raise CwError("Plan revision identity collision", ErrorCode.PLAN_REVISION_INVALID)
        return path, False
    atomic_json_new(path, validated)
    return path, True


def validate_revision_payload(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    schema_version(payload, "Plan revision")
    required = {
        "schema_version", "kind", "plan_revision_id", "canonical_workflow_sha256",
        "source_workflow_sha256", "parent_plan_revision_id", "created_at", "cw_version",
        "workflow_schema_version", "workflow_id", "goal", "completion_contract_sha256",
        "actor", "authorization_reference", "workflow",
    }
    if set(payload) != required or payload.get("kind") != "plan_revision":
        raise CwError("Plan revision schema is invalid", ErrorCode.PLAN_REVISION_INVALID)
    document = payload.get("workflow")
    if not isinstance(document, dict):
        raise CwError("Plan revision workflow is invalid", ErrorCode.PLAN_REVISION_INVALID)
    workflow = workflow_from_document(root, document)
    identifier = revision_id(document)
    canonical = canonical_document_hash(document)
    parent = payload.get("parent_plan_revision_id")
    actor = payload.get("actor")
    if (
        payload.get("plan_revision_id") != identifier
        or payload.get("canonical_workflow_sha256") != canonical
        or payload.get("workflow_id") != workflow.id
        or payload.get("goal") != workflow.goal
        or payload.get("workflow_schema_version") != document.get("schema_version")
        or not isinstance(payload.get("created_at"), str)
        or not isinstance(payload.get("cw_version"), str)
        or (parent is not None and (not isinstance(parent, str) or REVISION_ID.fullmatch(parent) is None))
        or not isinstance(actor, dict)
        or set(actor) != {"actor_id", "origin"}
        or not all(isinstance(actor.get(key), str) and actor[key] for key in actor)
    ):
        raise CwError("Plan revision is inconsistent", ErrorCode.PLAN_REVISION_INVALID)
    contract = document.get("completion_target")
    expected_contract = sha256_bytes(_canonical_bytes(contract)) if isinstance(contract, dict) else None
    if payload.get("completion_contract_sha256") != expected_contract:
        raise CwError("Plan revision Completion Contract hash is invalid", ErrorCode.PLAN_REVISION_INVALID)
    return payload


def load_revision(root: Path, identifier: str) -> dict[str, Any]:
    path = safe_file(revision_path(root, identifier), "Plan revision", required=True)
    return validate_revision_payload(root, load_json(path))


def workflow_for_revision(root: Path, identifier: str) -> Workflow:
    return workflow_from_document(root, load_revision(root, identifier)["workflow"])


def _criteria_contract(document: dict[str, Any], phase_id: str) -> bytes:
    for phase in document.get("phases", []):
        if isinstance(phase, dict) and phase.get("id") == phase_id:
            return _canonical_bytes({
                "acceptance_criteria": phase.get("acceptance_criteria", []),
                "blocking_criteria": phase.get("blocking_criteria", []),
                "artifacts": phase.get("artifacts", []),
                "review_paths": phase.get("review_paths", []),
                "required_commands": phase.get("required_commands", []),
            })
    raise CwError("Current phase is absent from proposed plan", ErrorCode.PLAN_REVISION_INVALID)


def _review_for_rebaseline(root: Path, workflow: Workflow, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    reference = state.get("last_review")
    if not isinstance(reference, str) or not reference.startswith(".cw/reviews/"):
        raise CwError("Rebaseline requires the active REVISE review", ErrorCode.PLAN_REBASELINE_REQUIRED)
    path = safe_project_path(root, reference, must_exist=True)
    if path.parent != root / ".cw" / "reviews" or path.is_symlink() or not path.is_file():
        raise CwError("Rebaseline review reference is unsafe", ErrorCode.PLAN_REBASELINE_REQUIRED)
    review = load_json(path)
    phase_id = state.get("current_phase")
    if review.get("workflow") != workflow.id or review.get("phase") != phase_id or review.get("decision") != ReviewDecision.REVISE.value:
        raise CwError("Rebaseline requires a REVISE review for the active phase", ErrorCode.PLAN_REBASELINE_REQUIRED)
    validate_reviewer_result(workflow.phase(str(phase_id)), review, root=root)
    active_id, active_hash = active_revision(root, state, workflow)
    if (
        review.get("plan_revision_id") not in {None, active_id}
        or review.get("canonical_workflow_sha256") not in {None, active_hash}
    ):
        raise CwError("REVISE review belongs to another plan revision", ErrorCode.SUPERSESSION_INVALID)
    return reference, review


def create_rebaseline_proposal(
    root: Path,
    current_workflow: Workflow,
    state: dict[str, Any],
    proposed_document: dict[str, Any],
    *,
    reason: str,
    actor_id: str,
    actor_origin: str,
) -> dict[str, Any]:
    if WorkflowState(str(state.get("status"))) is not WorkflowState.REVISION_REQUIRED:
        raise CwError("Plan rebaseline is allowed only after REVISE", ErrorCode.PLAN_REBASELINE_REQUIRED)
    if not reason.strip():
        raise CwError("Plan rebaseline requires a reason", ErrorCode.AUTHORIZATION_REQUIRED)
    phase_id = str(state.get("current_phase"))
    if _gate_path(root, phase_id).exists():
        raise CwError("A gated phase cannot be rebaselined", ErrorCode.INVALID_GATE)
    review_reference, review = _review_for_rebaseline(root, current_workflow, state)
    current_document = _read_document(root / ".codex/workflow/phases.yaml")
    proposed = copy.deepcopy(proposed_document)
    proposed.setdefault("workflow", {})["status"] = "APPROVED"
    proposed_workflow = workflow_from_document(root, proposed)
    if proposed_workflow.id != current_workflow.id:
        raise CwError("Proposed plan belongs to another project", ErrorCode.WORKFLOW_PROJECT_MISMATCH)
    current_index = current_workflow.index(phase_id)
    if tuple(phase.id for phase in proposed_workflow.phases) != tuple(phase.id for phase in current_workflow.phases):
        raise CwError("Rebaseline cannot add, remove, or reorder phases", ErrorCode.PLAN_REVISION_INVALID)
    for index in range(current_index):
        if _canonical_bytes(current_document["phases"][index]) != _canonical_bytes(proposed["phases"][index]):
            raise CwError("Rebaseline cannot alter an already approved phase", ErrorCode.INVALID_GATE)
    if _criteria_contract(current_document, phase_id) == _criteria_contract(proposed, phase_id):
        raise CwError("Rebaseline requires an explicit contract change", ErrorCode.PLAN_REBASELINE_REQUIRED)
    old_id, old_hash = active_revision(root, state, current_workflow)
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": "plan_rebaseline_proposal",
        "workflow": current_workflow.id,
        "phase": phase_id,
        "old_plan_revision_id": old_id,
        "old_plan_revision_sha256": old_hash,
        "new_plan_revision_id": revision_id(proposed),
        "new_plan_revision_sha256": canonical_document_hash(proposed),
        "review_reference": review_reference,
        "review_sha256": sha256_file(root / review_reference),
        "review_attempt": review.get("attempt"),
        "reason": reason.strip(),
        "actor": {"actor_id": actor_id, "origin": actor_origin},
        "created_at": utc_now(),
        "cw_version": __version__,
        "proposed_workflow": proposed,
    }
    identifier = proposal_id(body)
    payload = {**body, "proposal_id": identifier}
    payload["proposal_sha256"] = sha256_bytes(_canonical_bytes(payload))
    directory = safe_directory(root / ".cw" / "plan-proposals", ".cw/plan-proposals", create=True)
    path = directory / f"{identifier}.json"
    atomic_json_new(path, payload)
    state.setdefault("history", []).append({
        "timestamp": payload["created_at"], "phase": phase_id,
        "action": "plan_rebaseline_proposed", "proposal": path.relative_to(root).as_posix(),
        "old_plan_revision_id": old_id, "new_plan_revision_id": payload["new_plan_revision_id"],
    })
    state["pending_rebaseline"] = path.relative_to(root).as_posix()
    from .state import save_state

    save_state(root, state)
    return payload


def load_proposal(root: Path, identifier: str) -> dict[str, Any]:
    path = safe_file(proposal_path(root, identifier), "Plan rebaseline proposal", required=True)
    payload = load_json(path)
    schema_version(payload, "Plan rebaseline proposal")
    expected = proposal_id(payload)
    stored_hash = payload.get("proposal_sha256")
    unhashed = {key: value for key, value in payload.items() if key != "proposal_sha256"}
    required = {
        "schema_version", "kind", "workflow", "phase", "old_plan_revision_id",
        "old_plan_revision_sha256", "new_plan_revision_id", "new_plan_revision_sha256",
        "review_reference", "review_sha256", "review_attempt", "reason", "actor",
        "created_at", "cw_version", "proposed_workflow", "proposal_id", "proposal_sha256",
    }
    actor = payload.get("actor")
    review_attempt = payload.get("review_attempt")
    if (
        set(payload) != required
        or payload.get("kind") != "plan_rebaseline_proposal"
        or payload.get("proposal_id") != expected
        or stored_hash != sha256_bytes(_canonical_bytes(unhashed))
        or not isinstance(payload.get("workflow"), str)
        or not isinstance(payload.get("phase"), str)
        or REVISION_ID.fullmatch(str(payload.get("old_plan_revision_id"))) is None
        or REVISION_ID.fullmatch(str(payload.get("new_plan_revision_id"))) is None
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("old_plan_revision_sha256")))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("new_plan_revision_sha256")))
        or not isinstance(payload.get("review_reference"), str)
        or not payload["review_reference"].startswith(".cw/reviews/")
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("review_sha256")))
        or isinstance(review_attempt, bool)
        or not isinstance(review_attempt, int)
        or review_attempt < 1
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
        or not isinstance(actor, dict)
        or set(actor) != {"actor_id", "origin"}
        or not all(isinstance(actor.get(key), str) and actor[key] for key in actor)
        or not isinstance(payload.get("created_at"), str)
        or not isinstance(payload.get("cw_version"), str)
        or revision_id(payload.get("proposed_workflow", {})) != payload.get("new_plan_revision_id")
        or canonical_document_hash(payload.get("proposed_workflow", {})) != payload.get("new_plan_revision_sha256")
    ):
        raise CwError("Plan rebaseline proposal is invalid", ErrorCode.PLAN_REVISION_INVALID)
    return payload


def authorization_resource(proposal: dict[str, Any]) -> str:
    return f"{proposal['proposal_id']}:{proposal['proposal_sha256']}"


def _supersession_id(payload: dict[str, Any]) -> str:
    body = {
        key: value for key, value in payload.items()
        if key not in {"supersession_id", "supersession_sha256", "result"}
    }
    return "ps-" + sha256_bytes(_canonical_bytes(body)).removeprefix("sha256:")


def _transaction_path(root: Path) -> Path:
    return root / TRANSACTION


def recover_rebaseline_transaction(root: Path) -> dict[str, Any] | None:
    path = _transaction_path(root)
    if not path.exists():
        return None
    journal = load_json(safe_file(path, "Plan rebaseline transaction", required=True))
    if journal.get("kind") != "plan_rebaseline_transaction":
        raise CwError("Rebaseline transaction journal is corrupt", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    if journal.get("status") == "COMMITTED":
        path.unlink()
        return {"recovered": False, "committed": True, "operation_id": journal.get("operation_id")}
    old_plan = journal.get("old_workflow")
    old_state = journal.get("old_state")
    if not isinstance(old_plan, dict) or not isinstance(old_state, dict):
        raise CwError("Rebaseline transaction cannot be recovered", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    write_workflow(root / ".codex/workflow/phases.yaml", old_plan)
    atomic_json(root / ".cw/state.json", old_state)
    for reference in journal.get("created_files", []):
        if isinstance(reference, str):
            candidate = safe_project_path(root, reference)
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
    path.unlink()
    return {"recovered": True, "committed": False, "operation_id": journal.get("operation_id")}


FailureInjector = Callable[[str], None]


def apply_rebaseline(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
    proposal_identifier: str,
    context: OperationContext,
    *,
    failure_injector: FailureInjector | None = None,
) -> dict[str, Any]:
    recover_rebaseline_transaction(root)
    proposal = load_proposal(root, proposal_identifier)
    grant = validate_authorization(
        context.authorization,
        action="plan.rebaseline",
        resource_id=authorization_resource(proposal),
    )
    if context.requested_capability != "plan.rebaseline" or context.operation_id != grant.operation_id or context.actor != grant.actor:
        raise CwError("Rebaseline operation context is invalid", ErrorCode.AUTHORIZATION_REQUIRED)
    if (
        proposal.get("actor", {}).get("actor_id") != context.actor.actor_id
        or proposal.get("actor", {}).get("origin") != context.actor.origin.value
    ):
        raise CwError("Rebaseline proposal actor does not match authorization", ErrorCode.AUTHORIZATION_REQUIRED)
    for path in (root / ".cw/supersessions").glob("*.json"):
        existing = load_json(path)
        auth = existing.get("authorization") if isinstance(existing, dict) else None
        if isinstance(auth, dict) and auth.get("authorization_nonce") == grant.nonce:
            if existing.get("operation_id") == context.operation_id and existing.get("proposal_id") == proposal_identifier:
                return {**existing.get("result", {}), "idempotent_replay": True}
            raise CwError("Authorization nonce was already consumed", ErrorCode.OPERATION_CONFLICT)
        if isinstance(existing, dict) and existing.get("operation_id") == context.operation_id:
            raise CwError("Operation ID was already used", ErrorCode.OPERATION_CONFLICT)
    if WorkflowState(str(state.get("status"))) is not WorkflowState.REVISION_REQUIRED:
        raise CwError("Plan rebaseline is no longer allowed", ErrorCode.PLAN_REBASELINE_REQUIRED)
    old_id, old_hash = active_revision(root, state, workflow)
    if (old_id, old_hash) != (proposal.get("old_plan_revision_id"), proposal.get("old_plan_revision_sha256")):
        raise CwError("Active plan changed after proposal", ErrorCode.OPERATION_CONFLICT)
    review_reference, review = _review_for_rebaseline(root, workflow, state)
    if (
        review.get("plan_revision_id") not in {None, old_id}
        or review.get("canonical_workflow_sha256") not in {None, old_hash}
    ):
        raise CwError("REVISE review belongs to another plan revision", ErrorCode.SUPERSESSION_INVALID)
    if review_reference != proposal.get("review_reference") or sha256_file(root / review_reference) != proposal.get("review_sha256"):
        raise CwError("Reviewed evidence changed after proposal", ErrorCode.SUPERSESSION_INVALID)
    phase_id = str(state.get("current_phase"))
    if _gate_path(root, phase_id).exists():
        raise CwError("A gated phase cannot be rebaselined", ErrorCode.INVALID_GATE)
    proposed_document = proposal["proposed_workflow"]
    new_workflow = workflow_from_document(root, proposed_document)
    from .audit import audit_history
    from .initialize import backup_metadata
    from .state import save_state

    audit_history(root, workflow, state)
    backup = backup_metadata(root)
    old_document = _read_document(root / ".codex/workflow/phases.yaml")
    old_state = copy.deepcopy(state)
    old_revision = (
        load_revision(root, old_id)
        if revision_path(root, old_id).exists()
        else revision_payload(
            root, old_document, parent_revision_id=None,
            actor_id="legacy-migration", actor_origin="internal_supervisor",
        )
    )
    new_revision = revision_payload(
        root, proposed_document, parent_revision_id=old_id,
        actor_id=context.actor.actor_id, actor_origin=context.actor.origin.value,
        authorization_reference=proposal_path(root, proposal_identifier).relative_to(root).as_posix(),
    )
    created_at = utc_now()
    supersession = {
        "schema_version": SCHEMA_VERSION,
        "kind": "review_supersession",
        "workflow": workflow.id,
        "phase": phase_id,
        "review_reference": review_reference,
        "review_sha256": proposal["review_sha256"],
        "old_plan_revision_id": old_id,
        "old_plan_revision_sha256": old_hash,
        "new_plan_revision_id": proposal["new_plan_revision_id"],
        "new_plan_revision_sha256": proposal["new_plan_revision_sha256"],
        "proposal_id": proposal_identifier,
        "proposal_sha256": proposal["proposal_sha256"],
        "reason": proposal["reason"],
        "actor": {"actor_id": context.actor.actor_id, "origin": context.actor.origin.value},
        "authorization": grant.as_evidence(),
        "operation_id": context.operation_id,
        "created_at": created_at,
        "cw_version": __version__,
        "resulting_state": WorkflowState.READY.value,
    }
    result = {
        "status": "REBASELINED",
        "phase": phase_id,
        "old_plan_revision_id": old_id,
        "new_plan_revision_id": proposal["new_plan_revision_id"],
        "supersession": None,
        "backup": backup.relative_to(root).as_posix(),
        "global_attempt": int(state.get("attempt", 0)),
        "revision_attempt": 0,
        "gate": False,
    }
    supersession["result"] = result
    identifier = _supersession_id(supersession)
    target = supersession_path(root, identifier)
    result["supersession"] = target.relative_to(root).as_posix()
    supersession["supersession_id"] = identifier
    supersession["supersession_sha256"] = sha256_bytes(_canonical_bytes(supersession))
    planned_files = [
        path.relative_to(root).as_posix()
        for path in (
            revision_path(root, old_revision["plan_revision_id"]),
            revision_path(root, new_revision["plan_revision_id"]),
            target,
        )
        if not path.exists()
    ]
    journal = {
        "schema_version": SCHEMA_VERSION,
        "kind": "plan_rebaseline_transaction",
        "status": "PREPARED",
        "operation_id": context.operation_id,
        "old_workflow": old_document,
        "old_state": old_state,
        # Record every absent target before the first append-only write. A
        # crash between a write and its checkpoint can therefore be rolled
        # back without leaving an unreferenced revision or supersession.
        "created_files": planned_files,
        "backup": backup.relative_to(root).as_posix(),
        "created_at": utc_now(),
    }
    atomic_json(_transaction_path(root), journal)

    def step(name: str) -> None:
        journal["stage"] = name
        atomic_json(_transaction_path(root), journal)
        if failure_injector is not None:
            failure_injector(name)

    try:
        persist_revision(root, old_revision)
        step("old_revision_persisted")
        persist_revision(root, new_revision)
        step("new_revision_persisted")
        atomic_json_new(target, supersession)
        step("supersession_persisted")
        write_workflow(root / ".codex/workflow/phases.yaml", proposed_document)
        step("workflow_activated")
        state.update({
            "workflow_version": new_workflow.version,
            "workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml"),
            "active_plan_revision": proposal["new_plan_revision_id"],
            "active_plan_revision_sha256": proposal["new_plan_revision_sha256"],
            "superseded_plan_revisions": [
                *[item for item in state.get("superseded_plan_revisions", []) if isinstance(item, str)],
                old_id,
            ],
            "revision_attempt": 0,
            "status": WorkflowState.READY.value,
            "last_review": None,
            "last_error": None,
            "infrastructure_error": None,
            "pending_rebaseline": None,
        })
        state.setdefault("history", []).extend([
            {
                "timestamp": created_at, "phase": phase_id,
                "action": "plan_rebaseline_authorized", "proposal": proposal_path(root, proposal_identifier).relative_to(root).as_posix(),
                "operation_id": context.operation_id, "actor_id": context.actor.actor_id,
                "authorization_nonce": grant.nonce,
            },
            {
                "timestamp": created_at, "phase": phase_id,
                "action": "review_superseded", "review": review_reference,
                "supersession": target.relative_to(root).as_posix(),
                "old_plan_revision_id": old_id, "new_plan_revision_id": proposal["new_plan_revision_id"],
            },
            {
                "timestamp": created_at, "phase": phase_id,
                "action": "plan_revision_activated", "plan_revision_id": proposal["new_plan_revision_id"],
                "parent_plan_revision_id": old_id,
            },
        ])
        save_state(root, state)
        step("state_activated")
        audit_revisions(root, new_workflow, state)
        step("audit_completed")
        journal["status"] = "COMMITTED"
        journal["stage"] = "committed"
        atomic_json(_transaction_path(root), journal)
        _transaction_path(root).unlink()
        return result
    except BaseException:
        recover_rebaseline_transaction(root)
        raise


def validate_supersession(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    schema_version(payload, "Review supersession")
    required = {
        "schema_version", "kind", "workflow", "phase", "review_reference", "review_sha256",
        "old_plan_revision_id", "old_plan_revision_sha256", "new_plan_revision_id",
        "new_plan_revision_sha256", "proposal_id", "proposal_sha256", "reason", "actor",
        "authorization", "operation_id", "created_at", "cw_version", "resulting_state",
        "supersession_id", "supersession_sha256", "result",
    }
    if set(payload) != required or payload.get("kind") != "review_supersession":
        raise CwError("Review supersession schema is invalid", ErrorCode.SUPERSESSION_INVALID)
    identifier = _supersession_id(payload)
    unhashed = {key: value for key, value in payload.items() if key != "supersession_sha256"}
    if payload.get("supersession_id") != identifier or payload.get("supersession_sha256") != sha256_bytes(_canonical_bytes(unhashed)):
        raise CwError("Review supersession hash is invalid", ErrorCode.SUPERSESSION_INVALID)
    review = safe_project_path(root, str(payload.get("review_reference")), must_exist=True)
    if review.parent != root / ".cw/reviews" or review.is_symlink() or sha256_file(review) != payload.get("review_sha256"):
        raise CwError("Superseded review was altered", ErrorCode.SUPERSESSION_INVALID)
    old_revision = load_revision(root, str(payload.get("old_plan_revision_id")))
    new_revision = load_revision(root, str(payload.get("new_plan_revision_id")))
    if old_revision["canonical_workflow_sha256"] != payload.get("old_plan_revision_sha256") or new_revision["canonical_workflow_sha256"] != payload.get("new_plan_revision_sha256"):
        raise CwError("Supersession revision hash is invalid", ErrorCode.SUPERSESSION_INVALID)
    proposal = load_proposal(root, str(payload.get("proposal_id")))
    if (
        proposal["proposal_sha256"] != payload.get("proposal_sha256")
        or proposal.get("workflow") != payload.get("workflow")
        or proposal.get("phase") != payload.get("phase")
        or proposal.get("review_reference") != payload.get("review_reference")
        or proposal.get("review_sha256") != payload.get("review_sha256")
        or proposal.get("old_plan_revision_id") != payload.get("old_plan_revision_id")
        or proposal.get("old_plan_revision_sha256") != payload.get("old_plan_revision_sha256")
        or proposal.get("new_plan_revision_id") != payload.get("new_plan_revision_id")
        or proposal.get("new_plan_revision_sha256") != payload.get("new_plan_revision_sha256")
        or proposal.get("reason") != payload.get("reason")
        or new_revision.get("parent_plan_revision_id") != old_revision.get("plan_revision_id")
    ):
        raise CwError("Supersession proposal hash is invalid", ErrorCode.SUPERSESSION_INVALID)
    auth = payload.get("authorization")
    actor = payload.get("actor")
    if (
        not isinstance(auth, dict) or not isinstance(actor, dict)
        or auth.get("action") != "plan.rebaseline"
        or auth.get("resource_id") != authorization_resource(proposal)
        or auth.get("operation_id") != payload.get("operation_id")
        or auth.get("actor_id") != actor.get("actor_id")
        or auth.get("actor_origin") != actor.get("origin")
        or not isinstance(auth.get("authorization_nonce"), str)
        or auth.get("actor_origin") not in {
            "human_cli", "chatgpt_app", "codex_plugin",
        }
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
        or payload.get("workflow") != old_revision.get("workflow_id")
    ):
        raise CwError("Supersession authorization is invalid", ErrorCode.SUPERSESSION_INVALID)
    try:
        issued = datetime.fromisoformat(str(auth.get("issued_at")).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(auth.get("expires_at")).replace("Z", "+00:00"))
        created = datetime.fromisoformat(str(payload.get("created_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CwError("Supersession authorization time is invalid", ErrorCode.SUPERSESSION_INVALID) from exc
    if not issued <= created <= expires:
        raise CwError("Supersession authorization was not current", ErrorCode.SUPERSESSION_INVALID)
    old_workflow = workflow_from_document(root, old_revision["workflow"])
    review_data = load_json(review)
    phase = old_workflow.phase(str(payload.get("phase")))
    decision, _, _, _ = validate_reviewer_result(phase, review_data, root=root)
    if decision is not ReviewDecision.REVISE:
        raise CwError("Only a REVISE review can be superseded", ErrorCode.SUPERSESSION_INVALID)
    return payload


def supersession_index(root: Path) -> dict[str, dict[str, Any]]:
    directory = safe_directory(root / ".cw/supersessions", ".cw/supersessions")
    index: dict[str, dict[str, Any]] = {}
    nonces: set[str] = set()
    operations: set[str] = set()
    for path in sorted(directory.iterdir()):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix != ".json"
            or SUPERSESSION_ID.fullmatch(path.stem) is None
        ):
            raise CwError("Unexpected review supersession artifact", ErrorCode.SUPERSESSION_INVALID)
        payload = validate_supersession(root, load_json(path))
        if payload.get("supersession_id") != path.stem:
            raise CwError("Review supersession filename is inconsistent", ErrorCode.SUPERSESSION_INVALID)
        review = str(payload["review_reference"])
        nonce = str(payload["authorization"]["authorization_nonce"])
        operation = str(payload["operation_id"])
        if review in index or nonce in nonces or operation in operations:
            raise CwError("Duplicate review supersession", ErrorCode.SUPERSESSION_INVALID)
        index[review] = payload
        nonces.add(nonce)
        operations.add(operation)
    return index


def audit_validations(root: Path, workflow: Workflow, state: dict[str, Any]) -> int:
    directory = safe_directory(root / ".cw/validation", ".cw/validation")
    operations: set[str] = set()
    count = 0
    active_id, _ = active_revision(root, state, workflow)
    historical = set(state.get("superseded_plan_revisions", []))
    for path in sorted(directory.glob("*.json")):
        payload = load_json(path)
        schema_version(payload, f"Validation {path.name}")
        if payload.get("kind") not in {None, "phase_validation"}:
            raise CwError("Validation kind is invalid", ErrorCode.PLAN_REVISION_INVALID)
        if payload.get("workflow") != workflow.id or payload.get("phase") not in {phase.id for phase in workflow.phases}:
            raise CwError("Validation identity is invalid", ErrorCode.PLAN_REVISION_INVALID)
        operation = payload.get("operation_id")
        if not isinstance(operation, str) or not operation or operation in operations:
            raise CwError("Validation operation identity is invalid", ErrorCode.OPERATION_CONFLICT)
        operations.add(operation)
        if payload.get("kind") == "phase_validation":
            for key in ("validation_attempt", "revision_validation_attempt"):
                value = payload.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise CwError("Validation attempt identity is invalid", ErrorCode.PLAN_REVISION_INVALID)
        identifier = payload.get("plan_revision_id")
        if identifier is not None:
            if identifier not in historical | {active_id}:
                raise CwError("Validation references an unknown plan revision", ErrorCode.PLAN_REVISION_INVALID)
            if revision_path(root, identifier).exists():
                snapshot = load_revision(root, identifier)
                validation_workflow = workflow_from_document(root, snapshot["workflow"])
                expected_hash = snapshot["canonical_workflow_sha256"]
            else:
                validation_workflow = workflow
                expected_hash = active_revision(root, state, workflow)[1]
            if payload.get("canonical_workflow_sha256") != expected_hash:
                raise CwError("Validation plan revision hash is invalid", ErrorCode.PLAN_REVISION_INVALID)
        else:
            validation_workflow = workflow
        phase = validation_workflow.phase(str(payload["phase"]))
        hashes = payload.get("artifact_hashes")
        status = payload.get("status")
        if (
            status not in {"PASSED", "FAILED"}
            or not isinstance(hashes, dict)
            or (status == "PASSED" and set(hashes) != set(phase.artifacts))
            or (status == "FAILED" and not set(hashes).issubset(set(phase.artifacts)))
        ):
            raise CwError("Validation artifact manifest is invalid", ErrorCode.PLAN_REVISION_INVALID)
        count += 1
    return count


def validation_attempts(root: Path, workflow: Workflow, state: dict[str, Any]) -> tuple[int, int]:
    """Derive global and active-revision validation attempts from evidence."""

    audit_validations(root, workflow, state)
    active_id, _ = active_revision(root, state, workflow)
    global_attempt = 0
    revision_attempt = 0
    for path in sorted((root / ".cw/validation").glob("*.json")):
        payload = load_json(path)
        if payload.get("kind") != "phase_validation":
            continue
        global_attempt = max(global_attempt, int(payload["validation_attempt"]))
        if payload.get("plan_revision_id") == active_id:
            revision_attempt = max(revision_attempt, int(payload["revision_validation_attempt"]))
    return global_attempt, revision_attempt


def audit_revisions(root: Path, workflow: Workflow, state: dict[str, Any]) -> dict[str, Any]:
    active_id, active_hash = active_revision(root, state, workflow)
    explicit = state.get("active_plan_revision") is not None
    if explicit:
        active = load_revision(root, active_id)
        if active["canonical_workflow_sha256"] != active_hash or revision_id(_read_document(root / ".codex/workflow/phases.yaml")) != active_id:
            raise CwError("Active plan revision does not match workflow", ErrorCode.PLAN_REVISION_INVALID)
    index = supersession_index(root)
    validations = audit_validations(root, workflow, state)
    history = state.get("history", [])
    for review_reference, record in index.items():
        supersession_reference = str(record.get("result", {}).get("supersession"))
        authorized = any(
            isinstance(event, dict)
            and event.get("action") == "plan_rebaseline_authorized"
            and event.get("operation_id") == record.get("operation_id")
            and event.get("authorization_nonce") == record.get("authorization", {}).get("authorization_nonce")
            for event in history
        )
        superseded_event = any(
            isinstance(event, dict)
            and event.get("action") == "review_superseded"
            and event.get("review") == review_reference
            and event.get("supersession") == supersession_reference
            for event in history
        )
        activated = any(
            isinstance(event, dict)
            and event.get("action") == "plan_revision_activated"
            and event.get("plan_revision_id") == record.get("new_plan_revision_id")
            and event.get("parent_plan_revision_id") == record.get("old_plan_revision_id")
            for event in history
        )
        if not (authorized and superseded_event and activated):
            raise CwError("Review supersession is missing audit events", ErrorCode.SUPERSESSION_INVALID)
    superseded = state.get("superseded_plan_revisions", [])
    if (
        not isinstance(superseded, list)
        or not all(isinstance(item, str) and REVISION_ID.fullmatch(item) for item in superseded)
        or len(superseded) != len(set(superseded))
        or active_id in superseded
    ):
        raise CwError("Superseded plan revision state is invalid", ErrorCode.PLAN_REVISION_INVALID)
    revision_directory = safe_directory(root / ".cw/plan-revisions", ".cw/plan-revisions")
    observed_revisions: set[str] = set()
    for path in revision_directory.iterdir():
        identifier = path.stem
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix != ".json"
            or REVISION_ID.fullmatch(identifier) is None
        ):
            raise CwError("Unexpected plan revision artifact", ErrorCode.PLAN_REVISION_INVALID)
        if load_revision(root, identifier)["plan_revision_id"] != identifier:
            raise CwError("Plan revision filename is inconsistent", ErrorCode.PLAN_REVISION_INVALID)
        observed_revisions.add(identifier)
    expected_revisions = ({active_id} | set(superseded)) if explicit else set()
    if observed_revisions != expected_revisions:
        raise CwError("Plan revision artifacts are orphaned or missing", ErrorCode.PLAN_REVISION_INVALID)
    return {
        "active_plan_revision": active_id,
        "active_plan_revision_sha256": active_hash,
        "legacy_derived": not explicit,
        "superseded_reviews": len(index),
        "superseded_plan_revisions": list(superseded),
        "validations": validations,
    }


def review_revision(root: Path, workflow: Workflow, state: dict[str, Any], reference: str, review: dict[str, Any]) -> tuple[Workflow, str, bool]:
    index = supersession_index(root)
    supersession = index.get(reference)
    identifier = review.get("plan_revision_id")
    if supersession is not None:
        expected = supersession["old_plan_revision_id"]
        if identifier is not None and identifier != expected:
            raise CwError("Review revision conflicts with supersession", ErrorCode.SUPERSESSION_INVALID)
        return workflow_for_revision(root, expected), expected, True
    active_id, _ = active_revision(root, state, workflow)
    if identifier is None:
        return workflow, active_id, False
    if identifier != active_id:
        # An approval review may remain historical because its immutable gate
        # still authorizes an unchanged earlier phase in a later workflow
        # revision (for example an authorized Completion Contract extension).
        gated = any(
            isinstance(gate, dict) and gate.get("review_reference") == reference
            for path in (root / ".cw/gates").glob("*.json")
            for gate in [load_json(path)]
        )
        if not gated:
            raise CwError("Historical review has no supersession", ErrorCode.SUPERSESSION_INVALID)
        return workflow_for_revision(root, identifier), identifier, False
    if not revision_path(root, identifier).exists() and identifier == active_id:
        return workflow, identifier, False
    return workflow_for_revision(root, identifier), identifier, False
