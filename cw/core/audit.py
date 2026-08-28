from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .gates import validate_gate
from .layout import validate_tree
from .legacy_evidence import is_legacy_review, validate_legacy_review
from .models import Workflow
from .reviews import validate_reviewer_result
from .schema import schema_version
from .utils import load_json, safe_project_path, sha256_file

SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
EVENT_ACTIONS = {
    "approved", "human_approved", "human_review_required", "revision_required",
    "protected_path_violation", "reopened", "infrastructure_error",
    "infrastructure_error_migrated", "retry_started", "readiness_resume_started",
    "completion_contract_adopted", "completion_reviewed", "extension_proposed",
    "extension_approved", "extension_rejected",
    "plan_rebaseline_proposed", "plan_rebaseline_authorized",
    "review_superseded", "plan_revision_activated",
    "plan_amended",
    "phase_artifacts_amended",
    "rebaseline_recovery_applied",
    "semantic_review_superseded_as_infrastructure",
}


def _files(directory: Path, label: str) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise CwError(f"{label} directory is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    files: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.name == "archive" and entry.is_dir() and not entry.is_symlink():
            # Prototype gate reopen operations retained superseded evidence in
            # this repository-local archive. It is not an active approval set.
            validate_tree(entry, f"{label} archive")
            continue
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".json":
            raise CwError(f"Unexpected {label.lower()} entry: {entry.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        files.append(entry)
    return files


def _audit_review(path: Path, workflow: Workflow, state: dict[str, Any]) -> dict[str, Any]:
    data = load_json(path)
    schema_version(data, f"Review {path.name}")
    phase_id = data.get("phase")
    reference = path.relative_to(path.parents[2]).as_posix()
    from .revisions import review_revision

    review_workflow, resolved_revision, superseded = review_revision(
        path.parents[2], workflow, state, reference, data,
    )
    workflow_id = data.get("workflow_id") if is_legacy_review(data) else data.get("workflow")
    if workflow_id != review_workflow.id or phase_id not in {phase.id for phase in review_workflow.phases}:
        raise CwError(f"Review identity is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    phase = review_workflow.phase(str(phase_id))
    explicit_revision = data.get("plan_revision_id")
    if explicit_revision is not None and explicit_revision != resolved_revision:
        raise CwError(f"Review plan revision is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    attempt = data.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise CwError(f"Review attempt is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if not isinstance(data.get("created_at"), str) or not data["created_at"]:
        if is_legacy_review(data):
            validate_legacy_review(path.parents[2], workflow, phase, data)
            return data
        raise CwError(f"Review timestamp is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    kind = data.get("kind")
    if kind == "infrastructure_error":
        if not isinstance(data.get("error_code"), str) or not isinstance(data.get("error"), str):
            raise CwError(f"Infrastructure review is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        return data
    if kind != "semantic_review":
        raise CwError(f"Review kind is invalid: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    decision, criteria, blocking_criteria, issues = validate_reviewer_result(phase, data, root=path.parents[2])
    hashes = data.get("artifact_hashes")
    embedded_validation = data.get("validation_evidence")
    if (
        decision.value != data.get("decision")
        or criteria != data.get("criteria")
        or ("blocking_criteria" in data and blocking_criteria != data.get("blocking_criteria"))
        or issues != data.get("blocking_issues")
        or not isinstance(hashes, dict)
        or set(hashes) != set(phase.artifacts)
        or any(not isinstance(value, str) or SHA256.fullmatch(value) is None for value in hashes.values())
        or (
            embedded_validation is not None
            and (
                not isinstance(embedded_validation, dict)
                or embedded_validation.get("status") != "PASSED"
                or embedded_validation.get("artifact_hashes") != hashes
                or embedded_validation.get("plan_revision_id") != data.get("plan_revision_id")
                or embedded_validation.get("canonical_workflow_sha256") != data.get("canonical_workflow_sha256")
                or embedded_validation.get("candidate_sha") != data.get("candidate_sha")
            )
        )
    ):
        raise CwError(f"Semantic review is inconsistent: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return {**data, "_superseded": superseded, "_resolved_plan_revision_id": resolved_revision}


def audit_history(root: Path, workflow: Workflow, state: dict[str, Any]) -> dict[str, int]:
    review_files = _files(root / ".cw" / "reviews", "Reviews")
    gate_files = _files(root / ".cw" / "gates", "Gates")
    review_references: set[str] = set()
    for path in review_files:
        _audit_review(path, workflow, state)
        review_references.add(path.relative_to(root).as_posix())
    expected_gate_names = {f"{phase.id}.approved.json": phase.id for phase in workflow.phases}
    gate_references: set[str] = set()
    for path in gate_files:
        phase_id = expected_gate_names.get(path.name)
        if phase_id is None:
            raise CwError(f"Gate targets an unknown phase: {path.name}", ErrorCode.INVALID_GATE)
        gate = validate_gate(root, workflow, phase_id)
        review_reference = gate.get("review_reference") or gate.get("review_file")
        if review_reference not in review_references:
            raise CwError(f"Gate review reference is missing: {path.name}", ErrorCode.INVALID_GATE)
        gate_references.add(path.relative_to(root).as_posix())
    for key, known in (("last_review", review_references), ("last_gate", gate_references)):
        reference = state.get(key)
        if reference is not None:
            if not isinstance(reference, str):
                raise CwError(f"State {key} reference is invalid", ErrorCode.INVALID_STATE)
            safe_project_path(root, reference, must_exist=True)
            if reference not in known:
                raise CwError(f"State {key} reference is unknown", ErrorCode.INVALID_STATE)
    history = state.get("history")
    phase_ids = {phase.id for phase in workflow.phases}
    if not isinstance(history, list):
        raise CwError("Workflow history must be a list", ErrorCode.INVALID_STATE)
    for index, event in enumerate(history):
        action = event.get("action") if isinstance(event, dict) else None
        phase = event.get("phase") if isinstance(event, dict) else None
        phase_valid = phase in phase_ids or (
            action == "retry_started" and event.get("operation") == "planning" and phase is None
        ) or (
            action in {
                "completion_contract_adopted", "completion_reviewed", "extension_proposed",
                "extension_rejected", "plan_amended",
            }
            and phase is None
        )
        if (
            not isinstance(event, dict)
            or not phase_valid
            or action not in EVENT_ACTIONS
            or not isinstance(event.get("timestamp"), str)
            or not event["timestamp"]
        ):
            raise CwError(f"Workflow history event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action in {"approved", "human_review_required", "revision_required"}:
            attempt = event.get("attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise CwError(f"Workflow history attempt is invalid: {index}", ErrorCode.INVALID_STATE)
        if action in {"approved", "human_approved"}:
            gate_reference = event.get("gate")
            if not isinstance(gate_reference, str) or not gate_reference.startswith(".cw/gates/"):
                raise CwError(f"Workflow history gate is invalid: {index}", ErrorCode.INVALID_STATE)
            gate_file = safe_project_path(root, gate_reference)
            # Reopening deliberately removes the live gate while preserving its
            # audit event and backup. If a file remains, it must be a known gate.
            if gate_file.exists() and gate_reference not in gate_references:
                raise CwError(f"Workflow history gate is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "revision_required" and not isinstance(event.get("issues"), list):
            raise CwError(f"Workflow history issues are invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "reopened":
            backup = event.get("backup")
            if not isinstance(backup, str) or not backup.startswith(".cw/backups/"):
                raise CwError(f"Workflow history backup is invalid: {index}", ErrorCode.INVALID_STATE)
            receipt = event.get("receipt")
            if receipt is not None:
                if not isinstance(receipt, str) or not receipt.startswith(".cw/repair-receipts/"):
                    raise CwError(f"Workflow reopen receipt is invalid: {index}", ErrorCode.INVALID_STATE)
                receipt_payload = load_json(safe_project_path(root, receipt, must_exist=True))
                receipt_sha256 = event.get("receipt_sha256")
                if (
                    not isinstance(receipt_payload, dict)
                    or receipt_payload.get("kind") != "repair_reopen_receipt"
                    or receipt_payload.get("phase") != phase
                    or receipt_payload.get("backup") != backup
                    or not isinstance(receipt_payload.get("review_reference"), str)
                    or receipt_payload.get("review_reference") not in review_references
                    or not isinstance(receipt_payload.get("review_sha256"), str)
                    or SHA256.fullmatch(receipt_payload["review_sha256"]) is None
                    or not isinstance(receipt_sha256, str)
                    or SHA256.fullmatch(receipt_sha256) is None
                    or sha256_file(safe_project_path(root, receipt, must_exist=True)) != receipt_sha256
                    or sha256_file(safe_project_path(
                        root, receipt_payload["review_reference"], must_exist=True,
                    )) != receipt_payload["review_sha256"]
                ):
                    raise CwError(f"Workflow reopen receipt is inconsistent: {index}", ErrorCode.INVALID_STATE)
        if action == "rebaseline_recovery_applied":
            recovery = event.get("recovery")
            review = event.get("review")
            digest = event.get("review_sha256")
            backup = event.get("backup")
            operation_id = event.get("operation_id")
            correlation_id = event.get("correlation_id")
            event_id = event.get("event_id")
            if (
                not isinstance(recovery, str)
                or not recovery.startswith(".cw/rebaseline-recoveries/")
                or not isinstance(review, str)
                or review not in review_references
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
                or not isinstance(backup, str)
                or not backup.startswith(".cw/backups/")
                or not isinstance(operation_id, str)
                or not isinstance(correlation_id, str)
                or correlation_id != operation_id
                or not isinstance(event_id, str)
                or re.fullmatch(r"rre-[0-9a-f]{64}", event_id) is None
            ):
                raise CwError(f"Rebaseline recovery event is invalid: {index}", ErrorCode.INVALID_STATE)
            receipt = load_json(safe_project_path(root, recovery, must_exist=True))
            request = receipt.get("request") if isinstance(receipt, dict) else None
            transition = receipt.get("transition") if isinstance(receipt, dict) else None
            if (
                not isinstance(receipt, dict)
                or not isinstance(request, dict)
                or not isinstance(transition, dict)
                or receipt.get("kind") != "rebaseline_recovery_receipt"
                or request.get("phase") != phase
                or request.get("review_reference") != review
                or request.get("review_sha256") != digest
                or receipt.get("backup") != backup
                or receipt.get("operation_id") != operation_id
                or receipt.get("correlation_id") != correlation_id
                or transition.get("event_id") != event_id
            ):
                raise CwError(f"Rebaseline recovery event is inconsistent: {index}", ErrorCode.INVALID_STATE)
        if (
            action in {"infrastructure_error", "infrastructure_error_migrated"}
            and (not isinstance(event.get("error_code"), str) or not isinstance(event.get("operation"), str))
        ):
            raise CwError(f"Workflow infrastructure event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "semantic_review_superseded_as_infrastructure":
            from .review_infrastructure_recovery import (
                validate_review_infrastructure_recovery_receipt,
            )

            recovery = event.get("recovery_receipt")
            review = event.get("review")
            backup = event.get("backup")
            if (
                not isinstance(recovery, str)
                or not recovery.startswith(".cw/review-infrastructure-recoveries/")
                or not isinstance(review, str)
                or review not in review_references
                or not isinstance(backup, str)
                or not backup.startswith(".cw/backups/")
                or event.get("attempt_restored") != 1
            ):
                raise CwError(
                    f"Review infrastructure recovery event is invalid: {index}",
                    ErrorCode.INVALID_STATE,
                )
            recovery_path = safe_project_path(root, recovery, must_exist=True)
            receipt = load_json(recovery_path)
            if not isinstance(receipt, dict) or receipt.get("backup") != backup:
                raise CwError(
                    f"Review infrastructure recovery event is inconsistent: {index}",
                    ErrorCode.INVALID_STATE,
                )
            validate_review_infrastructure_recovery_receipt(
                root, recovery_path, receipt, require_current_state=False
            )
        if action in {"retry_started", "readiness_resume_started"} and not isinstance(event.get("operation"), str):
            raise CwError(f"Workflow retry event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "plan_rebaseline_proposed":
            from .revisions import load_proposal, proposal_path

            proposal = event.get("proposal")
            if not isinstance(proposal, str) or not proposal.startswith(".cw/plan-proposals/"):
                raise CwError(f"Plan rebaseline proposal event is invalid: {index}", ErrorCode.INVALID_STATE)
            loaded = load_proposal(root, Path(proposal).stem)
            canonical_proposal = proposal_path(root, loaded["proposal_id"]).relative_to(root).as_posix()
            if (
                proposal != canonical_proposal
                or loaded.get("phase") != phase
                or loaded.get("old_plan_revision_id") != event.get("old_plan_revision_id")
                or loaded.get("new_plan_revision_id") != event.get("new_plan_revision_id")
                or loaded.get("created_at") != event.get("timestamp")
            ):
                raise CwError(f"Plan rebaseline proposal event is inconsistent: {index}", ErrorCode.INVALID_STATE)
        if action == "plan_rebaseline_authorized" and (
            not isinstance(event.get("proposal"), str)
            or not isinstance(event.get("operation_id"), str)
            or not isinstance(event.get("actor_id"), str)
            or not isinstance(event.get("authorization_nonce"), str)
        ):
            raise CwError(f"Plan rebaseline authorization event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "review_superseded" and (
            not isinstance(event.get("review"), str)
            or not isinstance(event.get("supersession"), str)
            or not isinstance(event.get("old_plan_revision_id"), str)
            or not isinstance(event.get("new_plan_revision_id"), str)
        ):
            raise CwError(f"Review supersession event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "plan_revision_activated" and (
            not isinstance(event.get("plan_revision_id"), str)
            or not isinstance(event.get("parent_plan_revision_id"), str)
        ):
            raise CwError(f"Plan revision activation event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "plan_amended" and (
            not isinstance(event.get("previous_workflow_sha256"), str)
            or SHA256.fullmatch(event["previous_workflow_sha256"]) is None
            or not isinstance(event.get("workflow_sha256"), str)
            or SHA256.fullmatch(event["workflow_sha256"]) is None
            or not isinstance(event.get("completion_contract_sha256"), str)
            or not isinstance(event.get("backup"), str)
            or not event["backup"].startswith(".cw/backups/")
            or not isinstance(event.get("input_sha256"), str)
            or SHA256.fullmatch(event["input_sha256"]) is None
        ):
            raise CwError(f"Plan amendment event is invalid: {index}", ErrorCode.INVALID_STATE)
        if action == "plan_amended":
            from .completion import contract_hash

            backup = safe_project_path(root, event["backup"], must_exist=True)
            backup_workflow = backup / "phases.yaml"
            expected_contract = contract_hash(workflow.completion_target) if workflow.completion_target else "none"
            if (
                backup.is_symlink()
                or not backup.is_dir()
                or backup_workflow.is_symlink()
                or not backup_workflow.is_file()
                or sha256_file(backup_workflow) != event["previous_workflow_sha256"]
                or event["completion_contract_sha256"] != expected_contract
            ):
                raise CwError(f"Plan amendment evidence is inconsistent: {index}", ErrorCode.INVALID_STATE)
        if action == "phase_artifacts_amended":
            required = {
                "proposal_id", "previous_workflow_sha256", "workflow_sha256",
                "previous_state_sha256", "completion_contract_sha256",
                "previous_plan_revision", "new_plan_revision", "added_artifacts",
                "superseded_evidence", "backup", "reason", "operator",
            }
            if (
                not required.issubset(event)
                or not re.fullmatch(r"pa-[0-9a-f]{64}", str(event.get("proposal_id")))
                or any(SHA256.fullmatch(str(event.get(key))) is None for key in (
                    "previous_workflow_sha256", "workflow_sha256", "previous_state_sha256",
                ))
                or not isinstance(event.get("added_artifacts"), list)
                or not event["added_artifacts"]
                or not isinstance(event.get("superseded_evidence"), list)
                or not isinstance(event.get("reason"), str)
                or not event["reason"].strip()
            ):
                raise CwError(f"Active plan amendment event is invalid: {index}", ErrorCode.INVALID_STATE)
            from .completion import contract_hash
            from .revisions import load_revision

            backup = safe_project_path(root, str(event["backup"]), must_exist=True)
            previous = load_revision(root, str(event["previous_plan_revision"]))
            current = load_revision(root, str(event["new_plan_revision"]))
            previous_phase = next(
                (item for item in previous["workflow"]["phases"] if item.get("id") == phase), None,
            )
            current_phase = next(
                (item for item in current["workflow"]["phases"] if item.get("id") == phase), None,
            )
            old_artifacts = previous_phase.get("artifacts", []) if isinstance(previous_phase, dict) else []
            new_artifacts = current_phase.get("artifacts", []) if isinstance(current_phase, dict) else []
            normalized_current = copy.deepcopy(current["workflow"])
            normalized_current["workflow"]["status"] = previous["workflow"]["workflow"]["status"]
            normalized_phase = next(
                (item for item in normalized_current["phases"] if item.get("id") == phase), None,
            )
            if isinstance(normalized_phase, dict):
                normalized_phase["artifacts"] = copy.deepcopy(old_artifacts)
            expected_contract = contract_hash(workflow.completion_target) if workflow.completion_target else "none"
            if (
                backup.is_symlink()
                or not backup.is_dir()
                or sha256_file(backup / "phases.yaml") != event["previous_workflow_sha256"]
                or sha256_file(backup / "state.json") != event["previous_state_sha256"]
                or event["completion_contract_sha256"] != expected_contract
                or not isinstance(previous_phase, dict)
                or not isinstance(current_phase, dict)
                or not isinstance(normalized_phase, dict)
                or [value for value in new_artifacts if value not in old_artifacts] != event["added_artifacts"]
                or any(value not in new_artifacts for value in old_artifacts)
                or normalized_current != previous["workflow"]
            ):
                raise CwError(f"Active plan amendment evidence is inconsistent: {index}", ErrorCode.INVALID_STATE)
    from .completion import audit_completion_history
    from .plan_amendment import audit_evidence_supersessions
    from .rebaseline_recovery import _validate_recovery_receipts
    from .revisions import audit_revisions

    _validate_recovery_receipts(root, state)
    completion = audit_completion_history(root, workflow)
    revision_audit = audit_revisions(root, workflow, state)
    evidence_supersessions = audit_evidence_supersessions(root, workflow, state)
    result = {"reviews": len(review_files), "gates": len(gate_files), "events": len(history)}
    if not revision_audit["legacy_derived"] or revision_audit["superseded_reviews"]:
        result.update({
            "plan_revisions": 1 + len(revision_audit["superseded_plan_revisions"]),
            "superseded_reviews": revision_audit["superseded_reviews"],
        })
    if workflow.completion_target is not None or any(completion.values()):
        result.update(completion)
    if evidence_supersessions:
        result["superseded_evidence"] = evidence_supersessions
    return result
