from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cw import __version__
from .errors import CwError, ErrorCode
from .models import Phase, Workflow
from .reviews import validate_reviewer_result
from .schema import SCHEMA_VERSION, schema_version
from .utils import atomic_json_new, load_json, safe_project_path, sha256_file, utc_now


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
    if (
        not isinstance(review, dict)
        or review.get("workflow") != workflow.id
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
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    payload = {
        "schema_version": SCHEMA_VERSION, "cw_version": __version__, "workflow": workflow.id,
        "workflow_version": workflow.version, "phase": phase.id, "approved_at": utc_now(),
        "review_reference": review_reference, "artifact_hashes": artifact_hashes(root, phase.artifacts),
        "approval": {"kind": "human" if human_approved else "semantic"},
        "git": {"commit": commit.stdout.strip() or None},
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
    if (
        data.get("workflow") != workflow.id
        or data.get("workflow_version") != workflow.version
        or data.get("phase") != phase_id
        or not isinstance(data.get("cw_version"), str)
        or not isinstance(data.get("approved_at"), str)
        or not isinstance(data.get("git"), dict)
    ):
        raise CwError(f"Invalid approval gate: {phase_id}", ErrorCode.INVALID_GATE)
    try:
        phase = workflow.phase(phase_id)
    except KeyError as exc:
        raise CwError(f"Invalid approval gate: {phase_id}", ErrorCode.INVALID_GATE) from exc
    expected = data.get("artifact_hashes")
    if not isinstance(expected, dict) or set(expected) != set(phase.artifacts):
        raise CwError(f"Gate has no artifact hashes: {phase_id}", ErrorCode.INVALID_GATE)
    reference = data.get("review_reference")
    if not isinstance(reference, str):
        raise CwError(f"Gate has an invalid review reference: {phase_id}", ErrorCode.INVALID_GATE)
    review = validate_approval_review(root, workflow, phase, reference)
    if review.get("artifact_hashes") != expected:
        raise CwError(f"Gate review evidence is invalid: {phase_id}", ErrorCode.INVALID_GATE)
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
