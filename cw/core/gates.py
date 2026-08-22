from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cw import __version__
from .errors import CwError, ErrorCode
from .legacy_evidence import is_legacy_gate, is_legacy_review, validate_legacy_gate, validate_legacy_review
from .models import Phase, Workflow
from .reviews import validate_reviewer_result
from .schema import SCHEMA_VERSION, schema_version
from .utils import atomic_json_new, load_json, safe_project_path, sha256_file, utc_now
from .revisions import artifact_revision_metadata, revision_path, workflow_for_revision


def gate_path(root: Path, phase_id: str) -> Path:
    return root / ".cw" / "gates" / f"{phase_id}.approved.json"


def artifact_hashes(root: Path, artifacts: tuple[str, ...] | list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for value in artifacts:
        path = safe_project_path(root, value, must_exist=True)
        if not path.is_file():
            raise CwError(f"Artifact is not a regular file: {value}", ErrorCode.INVALID_GATE)
        hashes[value] = sha256_file(path)
    return hashes


def validate_approval_review(root: Path, workflow: Workflow, phase: Phase, reference: str) -> dict[str, Any]:
    if not reference.startswith(".cw/reviews/"):
        raise CwError(f"Gate has an invalid review reference: {phase.id}", ErrorCode.INVALID_GATE)
    review_path = safe_project_path(root, reference, must_exist=True)
    if review_path.parent != root / ".cw" / "reviews" or not review_path.is_file() or review_path.is_symlink():
        raise CwError(f"Gate has an invalid review reference: {phase.id}", ErrorCode.INVALID_GATE)
    review = load_json(review_path)
    schema_version(review, f"Review evidence for {phase.id}")
    if is_legacy_review(review):
        try:
            return validate_legacy_review(root, workflow, phase, review, require_approval=True)
        except CwError as exc:
            raise CwError(f"Gate review evidence is invalid: {phase.id}", ErrorCode.INVALID_GATE) from exc
    review_workflow = workflow
    review_revision_id = review.get("plan_revision_id")
    if review_revision_id is not None:
        if not isinstance(review_revision_id, str):
            raise CwError(f"Gate review revision is invalid: {phase.id}", ErrorCode.INVALID_GATE)
        try:
            review_workflow = workflow_for_revision(root, review_revision_id)
            phase = review_workflow.phase(phase.id)
        except (CwError, KeyError) as exc:
            raise CwError(f"Gate review revision is invalid: {phase.id}", ErrorCode.INVALID_GATE) from exc
    if (
        not isinstance(review, dict)
        or review.get("workflow") != review_workflow.id
        or review.get("phase") != phase.id
        or review.get("kind") != "semantic_review"
        or review.get("decision") not in {"APPROVE", "HUMAN_REVIEW_REQUIRED"}
    ):
        raise CwError(f"Gate review evidence is invalid: {phase.id}", ErrorCode.INVALID_GATE)
    try:
        decision, criteria, blocking_criteria, issues = validate_reviewer_result(phase, review, root=root)
    except CwError as exc:
        raise CwError(f"Gate review evidence is invalid: {phase.id}", ErrorCode.INVALID_GATE) from exc
    hashes = review.get("artifact_hashes")
    if (
        decision.value != review.get("decision")
        or criteria != review.get("criteria")
        or ("blocking_criteria" in review and blocking_criteria != review.get("blocking_criteria"))
        or issues != review.get("blocking_issues")
        or not isinstance(hashes, dict)
        or set(hashes) != set(phase.artifacts)
    ):
        raise CwError(f"Gate review evidence is inconsistent: {phase.id}", ErrorCode.INVALID_GATE)
    return review


def create_gate(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    review_reference: str,
    *,
    human_approved: bool = False,
) -> Path:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False, stdin=subprocess.DEVNULL,
        timeout=10,
    )
    from .state import load_state

    state = load_state(root)
    revision = artifact_revision_metadata(root, workflow, state)
    review = validate_approval_review(root, workflow, phase, review_reference)
    if review.get("plan_revision_id") is not None and review.get("plan_revision_id") != revision.get("plan_revision_id"):
        raise CwError("Gate cannot inherit approval from another plan revision", ErrorCode.INVALID_GATE)
    if review.get("candidate_sha") is not None and review.get("candidate_sha") != revision.get("candidate_sha"):
        raise CwError("Gate candidate does not match review candidate", ErrorCode.INVALID_GATE)
    payload = {
        "schema_version": SCHEMA_VERSION, "cw_version": __version__, "workflow": workflow.id,
        "workflow_version": workflow.version, "phase": phase.id, "approved_at": utc_now(),
        "review_reference": review_reference, "artifact_hashes": artifact_hashes(root, phase.artifacts),
        "approval": {"kind": "human" if human_approved else "semantic"},
        "git": {"commit": commit.stdout.strip() or None},
        **revision,
    }
    path = gate_path(root, phase.id)
    if path.exists():
        raise CwError(f"Approval gate already exists: {phase.id}", ErrorCode.INVALID_GATE, "Reopen the phase explicitly before reviewing it again.")
    try:
        atomic_json_new(path, payload)
    except FileExistsError as exc:
        raise CwError(f"Approval gate already exists: {phase.id}", ErrorCode.INVALID_GATE, "Reopen the phase explicitly before reviewing it again.") from exc
    return path


def validate_gate(root: Path, workflow: Workflow, phase_id: str) -> dict[str, Any]:
    path = gate_path(root, phase_id)
    if not path.is_file() or path.is_symlink():
        raise CwError(f"Missing dependency gate: {phase_id}", ErrorCode.INVALID_GATE)
    data = load_json(path)
    schema_version(data, f"Approval gate {phase_id}")
    try:
        phase = workflow.phase(phase_id)
    except KeyError as exc:
        raise CwError(f"Invalid approval gate: {phase_id}", ErrorCode.INVALID_GATE) from exc
    if is_legacy_gate(data):
        return validate_legacy_gate(root, workflow, phase, data)
    stored_gate_revision = data.get("plan_revision_id")
    gate_revision = stored_gate_revision
    gate_workflow = workflow
    gate_phase = phase
    if gate_revision is None:
        from .state import load_state

        state = load_state(root)
        for historical_id in state.get("superseded_plan_revisions", []):
            historical = workflow_for_revision(root, historical_id)
            if historical.id != data.get("workflow") or historical.version != data.get("workflow_version"):
                continue
            try:
                historical_phase = historical.phase(phase_id)
            except KeyError:
                continue
            if isinstance(data.get("artifact_hashes"), dict) and set(data["artifact_hashes"]) == set(historical_phase.artifacts):
                gate_revision = historical_id
                gate_workflow = historical
                gate_phase = historical_phase
                break
    if gate_revision is not None:
        if not isinstance(gate_revision, str):
            raise CwError(f"Gate plan revision is invalid: {phase_id}", ErrorCode.INVALID_GATE)
        gate_workflow = workflow_for_revision(root, gate_revision)
        try:
            gate_phase = gate_workflow.phase(phase_id)
        except KeyError as exc:
            raise CwError(f"Gate revision lacks phase: {phase_id}", ErrorCode.INVALID_GATE) from exc
    if (
        data.get("workflow") != gate_workflow.id
        or data.get("workflow_version") != gate_workflow.version
        or data.get("phase") != phase_id
        or not isinstance(data.get("cw_version"), str)
        or not isinstance(data.get("approved_at"), str)
        or not isinstance(data.get("git"), dict)
    ):
        raise CwError(f"Invalid approval gate: {phase_id}", ErrorCode.INVALID_GATE)
    expected = data.get("artifact_hashes")
    if not isinstance(expected, dict) or set(expected) != set(gate_phase.artifacts):
        raise CwError(f"Gate has no artifact hashes: {phase_id}", ErrorCode.INVALID_GATE)
    reference = data.get("review_reference")
    if not isinstance(reference, str):
        raise CwError(f"Gate has an invalid review reference: {phase_id}", ErrorCode.INVALID_GATE)
    review = validate_approval_review(root, workflow, phase, reference)
    review_revision = review.get("plan_revision_id")
    if stored_gate_revision is not None:
        if not isinstance(gate_revision, str) or review_revision not in {None, gate_revision}:
            raise CwError(f"Gate and review plan revisions differ: {phase_id}", ErrorCode.INVALID_GATE)
        from .revisions import canonical_document_hash
        from .workflow import _read_document

        historical_document = load_json(revision_path(root, gate_revision))["workflow"]
        active_document = _read_document(root / ".codex/workflow/phases.yaml")
        historical_contract = next(item for item in historical_document["phases"] if item.get("id") == phase_id)
        active_contract = next(item for item in active_document["phases"] if item.get("id") == phase_id)
        if canonical_document_hash({"phase": historical_contract}) != canonical_document_hash({"phase": active_contract}):
            raise CwError(f"Gate phase contract changed across plan revisions: {phase_id}", ErrorCode.INVALID_GATE)
        if data.get("canonical_workflow_sha256") != load_json(revision_path(root, gate_revision)).get("canonical_workflow_sha256"):
            raise CwError(f"Gate plan revision hash is invalid: {phase_id}", ErrorCode.INVALID_GATE)
        if data.get("candidate_sha") != review.get("candidate_sha"):
            raise CwError(f"Gate candidate differs from review candidate: {phase_id}", ErrorCode.INVALID_GATE)
    if review.get("artifact_hashes") != expected:
        raise CwError(f"Gate review evidence is invalid: {phase_id}", ErrorCode.INVALID_GATE)
    validation_reference = review.get("validation_reference")
    if validation_reference is not None:
        if not isinstance(validation_reference, str) or not validation_reference.startswith(".cw/validation/"):
            raise CwError(f"Gate validation reference is invalid: {phase_id}", ErrorCode.INVALID_GATE)
        validation_path = safe_project_path(root, validation_reference, must_exist=True)
        if validation_path.parent != root / ".cw/validation" or validation_path.is_symlink():
            raise CwError(f"Gate validation reference is invalid: {phase_id}", ErrorCode.INVALID_GATE)
        validation = load_json(validation_path)
        if (
            validation.get("kind") != "phase_validation"
            or validation.get("status") != "PASSED"
            or validation.get("workflow") != workflow.id
            or validation.get("phase") != phase_id
            or validation.get("plan_revision_id") != review.get("plan_revision_id")
            or validation.get("canonical_workflow_sha256") != review.get("canonical_workflow_sha256")
            or validation.get("candidate_sha") != review.get("candidate_sha")
            or validation.get("artifact_hashes") != review.get("artifact_hashes")
        ):
            raise CwError(f"Gate validation and review differ: {phase_id}", ErrorCode.INVALID_GATE)
    embedded_validation = review.get("validation_evidence")
    if embedded_validation is not None and (
        not isinstance(embedded_validation, dict)
        or embedded_validation.get("status") != "PASSED"
        or embedded_validation.get("artifact_hashes") != review.get("artifact_hashes")
        or embedded_validation.get("plan_revision_id") != review.get("plan_revision_id")
        or embedded_validation.get("canonical_workflow_sha256") != review.get("canonical_workflow_sha256")
        or embedded_validation.get("candidate_sha") != review.get("candidate_sha")
    ):
        raise CwError(f"Gate embedded validation and review differ: {phase_id}", ErrorCode.INVALID_GATE)
    approval = data.get("approval")
    kind = approval.get("kind") if isinstance(approval, dict) else "semantic" if approval is None else None
    if kind not in {"semantic", "human"}:
        raise CwError(f"Gate approval type is invalid: {phase_id}", ErrorCode.INVALID_GATE)
    human_required = phase.requires_human_approval or review.get("decision") == "HUMAN_REVIEW_REQUIRED"
    if human_required and kind != "human":
        # v0.1.0/0.1.1 gates predate the explicit approval marker. Preserve
        # those only when their append-only history records the human action.
        state = load_json(root / ".cw" / "state.json")
        gate_reference = path.relative_to(root).as_posix()
        legacy_human = isinstance(state, dict) and any(
            isinstance(event, dict)
            and event.get("phase") == phase_id
            and event.get("action") == "human_approved"
            and event.get("gate") == gate_reference
            for event in state.get("history", [])
        )
        if approval is not None or not legacy_human:
            raise CwError(f"Gate is missing required human approval: {phase_id}", ErrorCode.INVALID_GATE)
    current = artifact_hashes(root, list(expected))
    if current != expected:
        changed = sorted(name for name in set(current) | set(expected) if current.get(name) != expected.get(name))
        raise CwError(
            "Approval gate invalidated", ErrorCode.INVALID_GATE,
            "Re-open and review the affected phase explicitly.", details=f"Phase: {phase_id}\nChanged: {', '.join(changed)}",
        )
    return data


def validate_dependencies(root: Path, workflow: Workflow, phase: Phase) -> None:
    for dependency in phase.depends_on:
        validate_gate(root, workflow, dependency)
