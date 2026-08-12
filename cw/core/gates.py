from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cw import __version__
from .errors import CwError, ErrorCode
from .models import Phase, Workflow
from .utils import atomic_json, load_json, safe_project_path, sha256_file, utc_now


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


def create_gate(root: Path, workflow: Workflow, phase: Phase, review_reference: str) -> Path:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    payload = {
        "schema_version": 1, "cw_version": __version__, "workflow": workflow.id,
        "workflow_version": workflow.version, "phase": phase.id, "approved_at": utc_now(),
        "review_reference": review_reference, "artifact_hashes": artifact_hashes(root, phase.artifacts),
        "git": {"commit": commit.stdout.strip() or None},
    }
    path = gate_path(root, phase.id)
    if path.exists():
        raise CwError(f"Approval gate already exists: {phase.id}", ErrorCode.INVALID_GATE, "Reopen the phase explicitly before reviewing it again.")
    atomic_json(path, payload)
    return path


def validate_gate(root: Path, workflow: Workflow, phase_id: str) -> dict[str, Any]:
    path = gate_path(root, phase_id)
    if not path.is_file():
        raise CwError(f"Missing dependency gate: {phase_id}", ErrorCode.INVALID_GATE)
    data = load_json(path)
    if not isinstance(data, dict) or data.get("workflow") != workflow.id or data.get("phase") != phase_id:
        raise CwError(f"Invalid approval gate: {phase_id}", ErrorCode.INVALID_GATE)
    expected = data.get("artifact_hashes")
    if not isinstance(expected, dict):
        raise CwError(f"Gate has no artifact hashes: {phase_id}", ErrorCode.INVALID_GATE)
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
