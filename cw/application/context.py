from __future__ import annotations

from pathlib import Path
from typing import Any

from cw.core.config import apply_policy, load_policy
from cw.core.errors import CwError, ErrorCode
from cw.core.layout import validate_project_layout
from cw.core.project import load_project
from cw.core.state import load_state, validate_state
from cw.core.workflow import load_workflow


def load_project_context(root: Path, *, validate: bool = True) -> tuple[Any, dict[str, Any], Any]:
    """Load the shared CW source of truth without any terminal behavior."""

    validate_project_layout(root)
    project = load_project(root)
    workflow = load_workflow(root)
    if workflow.id != project.project_id or workflow.repository != project.project_id:
        raise CwError(
            "Project workflow mismatch",
            ErrorCode.WORKFLOW_PROJECT_MISMATCH,
            "Run: cw repair",
            details=(
                f"Workflow: {workflow.repository or workflow.id}\n"
                f"Repository: {project.project_id}"
            ),
        )
    workflow = apply_policy(workflow, load_policy(root, workflow=workflow))
    state = load_state(root)
    if validate and workflow.phases:
        validate_state(root, state, workflow)
    return project, state, workflow
