from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .commands import command_arguments
from .errors import CwError, ErrorCode
from .layout import safe_file
from .models import CompletionContract, PlanStatus, Workflow
from .schema import schema_version
from .severity import CANONICAL_CRITERION_SEVERITIES
from .utils import atomic_write, safe_project_path, sha256_bytes


def _read_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]
            value = yaml.safe_load(text)
        except (ImportError, Exception) as exc:
            raise CwError("phases.yaml is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, details=str(exc)) from exc
    if not isinstance(value, dict):
        raise CwError("phases.yaml must contain an object", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return value


def workflow_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_workflow(root: Path, *, allow_empty: bool = True) -> Workflow:
    path = root / ".codex" / "workflow" / "phases.yaml"
    safe_file(path, ".codex/workflow/phases.yaml")
    if not path.is_file():
        if allow_empty:
            raise CwError("Plan has not been created.", ErrorCode.INVALID_STATE, "Run: cw plan")
        raise CwError("Missing phases.yaml", ErrorCode.SCHEMA_VALIDATION_ERROR)
    data = _read_document(path)
    return workflow_from_document(root, data)


def workflow_from_document(root: Path, data: dict[str, Any]) -> Workflow:
    """Parse and strictly validate an already-loaded canonical workflow."""
    schema_version(data, "Workflow plan")
    meta = data.get("workflow", {})
    settings = data.get("settings", {})
    reviewer = data.get("reviewer", {})
    phases_data = data.get("phases", [])
    if not isinstance(meta, dict) or not isinstance(phases_data, list):
        raise CwError("Invalid workflow structure", ErrorCode.SCHEMA_VALIDATION_ERROR)
    try:
        phases = tuple(__import__("cw.core.models", fromlist=["Phase"]).Phase.from_dict(item) for item in phases_data)
        workflow = Workflow(
            id=str(meta.get("id", "")), repository=str(meta.get("repository", "")),
            version=int(meta.get("version", 1)), status=str(meta.get("status", "PROPOSED")),
            goal=str(meta["goal"]) if meta.get("goal") else None, phases=phases,
            max_review_attempts=int(settings.get("max_review_attempts", 3)),
            command_timeout=int(settings.get("command_timeout_seconds", 1200)),
            review_timeout=int(reviewer.get("timeout_seconds", 1200)),
            completion_target=(
                CompletionContract.from_dict(data["completion_target"])
                if isinstance(data.get("completion_target"), dict) else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CwError("phases.yaml has invalid field types", ErrorCode.SCHEMA_VALIDATION_ERROR, details=str(exc)) from exc
    validate_workflow(root, workflow)
    return workflow


def validate_workflow(root: Path, workflow: Workflow) -> None:
    try:
        PlanStatus(workflow.status)
    except ValueError as exc:
        raise CwError("Workflow plan status is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc
    if not workflow.id or workflow.id != workflow.repository:
        raise CwError("Workflow identity is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    ids = [phase.id for phase in workflow.phases]
    if len(ids) != len(set(ids)):
        raise CwError("Phase IDs must be unique", ErrorCode.SCHEMA_VALIDATION_ERROR)
    known: set[str] = set()
    criteria: set[str] = set()
    for phase in workflow.phases:
        if not phase.id or not phase.name or not phase.objective:
            raise CwError("Every phase needs id, name, and objective", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if not re.fullmatch(r"[0-9]{2,4}-[a-z0-9][a-z0-9-]*", phase.id):
            raise CwError(f"Phase ID is invalid: {phase.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if any(dep not in known for dep in phase.depends_on):
            raise CwError(f"Phase {phase.id} has a missing or future dependency", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if len(phase.depends_on) != len(set(phase.depends_on)):
            raise CwError(f"Phase {phase.id} has duplicate dependencies", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if len(phase.artifacts) != len(set(phase.artifacts)):
            raise CwError(f"Phase {phase.id} has duplicate artifacts", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if len(phase.review_paths) != len(set(phase.review_paths)):
            raise CwError(f"Phase {phase.id} has duplicate review paths", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if len(phase.required_integrations) != len(set(phase.required_integrations)) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) for value in phase.required_integrations
        ):
            raise CwError(f"Phase {phase.id} has invalid required integrations", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if len(phase.expected_evidence) != len(set(phase.expected_evidence)) or any(
            not value.strip() for value in phase.expected_evidence
        ):
            raise CwError(f"Phase {phase.id} has invalid expected evidence", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for artifact in phase.artifacts:
            if any(char in artifact for char in "*?["):
                raise CwError(f"Phase {phase.id} artifact cannot be a glob", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for value in (*phase.artifacts, *phase.review_paths):
            normalized = value.replace("\\", "/").removeprefix("./")
            if normalized.split("/", 1)[0] in {".cw", ".codex", ".git"}:
                raise CwError(f"Phase {phase.id} targets protected workflow metadata", ErrorCode.SCHEMA_VALIDATION_ERROR)
            safe_project_path(root, value)
        for criterion in phase.acceptance_criteria:
            if criterion.severity.value not in CANONICAL_CRITERION_SEVERITIES:
                raise CwError(f"Criterion severity is invalid: {criterion.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
            if criterion.id in criteria:
                raise CwError(f"Duplicate criterion: {criterion.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
            criteria.add(criterion.id)
        if not phase.acceptance_criteria:
            raise CwError(f"Phase {phase.id} has no acceptance criteria", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if any(not value.strip() for value in phase.blocking_criteria) or len(phase.blocking_criteria) != len(set(phase.blocking_criteria)):
            raise CwError(f"Phase {phase.id} has invalid blocking criteria", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for command in phase.required_commands:
            command_arguments(command.command)
            if command.timeout_seconds is not None and command.timeout_seconds <= 0:
                raise CwError(
                    f"Required command timeout must be positive in phase {phase.id}",
                    ErrorCode.SCHEMA_VALIDATION_ERROR,
                )
        known.add(phase.id)
    contract = workflow.completion_target
    if contract is not None:
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]*", contract.id)
            or not contract.name.strip()
            or not contract.description.strip()
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", contract.target_type)
            or not contract.requirements
        ):
            raise CwError("Completion Contract is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        requirement_ids = [item.id for item in contract.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise CwError("Completion requirement IDs must be unique", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for requirement in contract.requirements:
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", requirement.id)
                or not requirement.description.strip()
                or not requirement.evidence_expectations
                or any(not value.strip() for value in requirement.evidence_expectations)
            ):
                raise CwError(
                    f"Completion requirement is invalid: {requirement.id}",
                    ErrorCode.SCHEMA_VALIDATION_ERROR,
                )
        known_requirements = set(requirement_ids)
        for phase in workflow.phases:
            if len(phase.completion_requirements) != len(set(phase.completion_requirements)) or any(
                value not in known_requirements for value in phase.completion_requirements
            ):
                raise CwError(
                    f"Phase {phase.id} has invalid completion requirement links",
                    ErrorCode.SCHEMA_VALIDATION_ERROR,
                )


def write_workflow(path: Path, payload: dict[str, Any]) -> None:
    # JSON is a strict YAML subset and avoids a mandatory runtime dependency.
    atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def set_plan_status(root: Path, status: str) -> None:
    try:
        canonical = PlanStatus(status).value
    except ValueError as exc:
        raise CwError("Workflow plan status is invalid", ErrorCode.INVALID_STATE) from exc
    path = root / ".codex" / "workflow" / "phases.yaml"
    data = _read_document(path)
    data.setdefault("workflow", {})["status"] = canonical
    write_workflow(path, data)
