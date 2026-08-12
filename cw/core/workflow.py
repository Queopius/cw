from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .models import Workflow
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
    if not path.is_file():
        if allow_empty:
            raise CwError("Plan has not been created.", ErrorCode.INVALID_STATE, "Run: cw plan")
        raise CwError("Missing phases.yaml", ErrorCode.SCHEMA_VALIDATION_ERROR)
    data = _read_document(path)
    meta = data.get("workflow", {})
    settings = data.get("settings", {})
    reviewer = data.get("reviewer", {})
    phases_data = data.get("phases", [])
    if not isinstance(meta, dict) or not isinstance(phases_data, list):
        raise CwError("Invalid workflow structure", ErrorCode.SCHEMA_VALIDATION_ERROR)
    phases = tuple(__import__("cw.core.models", fromlist=["Phase"]).Phase.from_dict(item) for item in phases_data)
    workflow = Workflow(
        id=str(meta.get("id", "")), repository=str(meta.get("repository", "")),
        version=int(meta.get("version", 1)), status=str(meta.get("status", "PROPOSED")),
        goal=str(meta["goal"]) if meta.get("goal") else None, phases=phases,
        max_review_attempts=int(settings.get("max_review_attempts", 3)),
        command_timeout=int(settings.get("command_timeout_seconds", 1200)),
        review_timeout=int(reviewer.get("timeout_seconds", 1200)),
    )
    validate_workflow(root, workflow)
    return workflow


def validate_workflow(root: Path, workflow: Workflow) -> None:
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
        if any(dep not in known for dep in phase.depends_on):
            raise CwError(f"Phase {phase.id} has a missing or future dependency", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for value in (*phase.artifacts, *phase.review_paths):
            if not any(char in value for char in "*?["):
                safe_project_path(root, value)
        for criterion in phase.acceptance_criteria:
            if criterion.id in criteria:
                raise CwError(f"Duplicate criterion: {criterion.id}", ErrorCode.SCHEMA_VALIDATION_ERROR)
            criteria.add(criterion.id)
        if not phase.acceptance_criteria:
            raise CwError(f"Phase {phase.id} has no acceptance criteria", ErrorCode.SCHEMA_VALIDATION_ERROR)
        known.add(phase.id)


def write_workflow(path: Path, payload: dict[str, Any]) -> None:
    # JSON is a strict YAML subset and avoids a mandatory runtime dependency.
    atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def set_plan_status(root: Path, status: str) -> None:
    path = root / ".codex" / "workflow" / "phases.yaml"
    data = _read_document(path)
    data.setdefault("workflow", {})["status"] = status
    write_workflow(path, data)
