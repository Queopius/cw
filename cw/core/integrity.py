from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .gates import artifact_hashes, validate_gate
from .models import Phase, ReviewDecision, Workflow, WorkflowState
from .reviews import validate_reviewer_result
from .schema import schema_version
from .state import load_state
from .utils import load_json, safe_project_path, sha256_file
from .utils import sha256_bytes


@dataclass(frozen=True, slots=True)
class ProtectedSnapshot:
    roots: tuple[str, ...]
    entries: dict[str, str]
    state: dict[str, Any] | None
    phase_contract: str | None = None


def phase_contract_fingerprint(workflow: Workflow, phase: Phase) -> str:
    """Hash immutable semantic inputs for one implementation session.

    Operational metadata such as project/state writer versions and timestamps
    is deliberately absent. Those files remain separately protected against
    implementation-agent writes by the managed-metadata snapshot.
    """
    import json

    payload = {
        "workflow": workflow.id,
        "workflow_version": workflow.version,
        "phase": {
            "id": phase.id,
            "name": phase.name,
            "objective": phase.objective,
            "depends_on": list(phase.depends_on),
            "artifacts": list(phase.artifacts),
            "review_paths": list(phase.review_paths),
            "required_commands": [
                {"command": command.command, "timeout_seconds": command.timeout_seconds}
                for command in phase.required_commands
            ],
            "acceptance_criteria": [
                {"id": criterion.id, "description": criterion.description, "severity": criterion.severity.value}
                for criterion in phase.acceptance_criteria
            ],
            "blocking_criteria": list(phase.blocking_criteria),
            "requires_human_approval": phase.requires_human_approval,
            "required_integrations": list(phase.required_integrations),
        },
        "policy": {
            "max_review_attempts": workflow.max_review_attempts,
            "allow_network": workflow.allow_network,
            "command_timeout": workflow.command_timeout,
            "review_timeout": workflow.review_timeout,
            "protected_paths": list(workflow.protected_paths),
            "human_gate_categories": list(workflow.human_gate_categories),
        },
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _normalized_root(root: Path, value: str) -> tuple[str, Path]:
    relative = Path(value).as_posix().removeprefix("./")
    if any(character in relative for character in "*?["):
        raise CwError(f"Protected path cannot be a glob: {value}", ErrorCode.USAGE_ERROR, exit_code=2)
    path = safe_project_path(root, relative)
    if path.is_symlink():
        raise CwError(f"Protected path cannot be a symlink: {value}", ErrorCode.USAGE_ERROR, exit_code=2)
    return relative, path


def snapshot_protected_paths(
    root: Path,
    paths: tuple[str, ...],
    *,
    workflow: Workflow | None = None,
    phase: Phase | None = None,
) -> ProtectedSnapshot:
    entries: dict[str, str] = {}
    normalized: list[str] = []
    state: dict[str, Any] | None = None
    for value in paths:
        relative, path = _normalized_root(root, value)
        normalized.append(relative)
        if not path.exists():
            entries[relative] = "missing"
            continue
        if path.is_file():
            entries[relative] = sha256_file(path)
            if relative == ".cw/state.json":
                loaded = load_json(path)
                if not isinstance(loaded, dict):
                    raise CwError("Workflow state is invalid", ErrorCode.INVALID_STATE)
                state = loaded
            continue
        if not path.is_dir():
            raise CwError(f"Protected path is not a regular file or directory: {value}", ErrorCode.USAGE_ERROR, exit_code=2)
        entries[f"{relative}/"] = "directory"
        for child in sorted(path.rglob("*")):
            child_relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                raise CwError(f"Protected path contains a symlink: {child_relative}", ErrorCode.PROTECTED_PATH_MODIFIED)
            if child.is_dir():
                entries[f"{child_relative}/"] = "directory"
            elif child.is_file():
                entries[child_relative] = sha256_file(child)
            else:
                raise CwError(f"Protected path contains a special file: {child_relative}", ErrorCode.PROTECTED_PATH_MODIFIED)
    contract = phase_contract_fingerprint(workflow, phase) if workflow is not None and phase is not None else None
    return ProtectedSnapshot(tuple(normalized), entries, state, contract)


def _is_new_review(path: str) -> bool:
    candidate = Path(path)
    return candidate.parent.as_posix() == ".cw/reviews" and candidate.suffix == ".json"


def _is_new_gate(path: str) -> bool:
    candidate = Path(path)
    return candidate.parent.as_posix() == ".cw/gates" and candidate.name.endswith(".approved.json")


def _validate_review(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    state: dict[str, Any],
    reference: str,
) -> dict[str, Any]:
    if reference != state.get("last_review"):
        raise CwError("Protected review was not recorded in workflow state", ErrorCode.PROTECTED_PATH_MODIFIED)
    report = load_json(safe_project_path(root, reference, must_exist=True))
    schema_version(report, "Protected review")
    if (
        not isinstance(report, dict)
        or report.get("workflow") != workflow.id
        or report.get("phase") != phase.id
        or not isinstance(report.get("attempt"), int)
        or report["attempt"] <= 0
    ):
        raise CwError("Protected review metadata is invalid", ErrorCode.PROTECTED_PATH_MODIFIED)
    kind = report.get("kind")
    status = WorkflowState(str(state["status"]))
    if kind == "infrastructure_error":
        if status is not WorkflowState.ERROR or report["attempt"] != int(state.get("attempt", 0)) + 1:
            raise CwError("Infrastructure review state is inconsistent", ErrorCode.PROTECTED_PATH_MODIFIED)
        code = report.get("error_code")
        if not isinstance(code, str) or not str(state.get("last_error") or "").startswith(f"{code}:"):
            raise CwError("Infrastructure review error is inconsistent", ErrorCode.PROTECTED_PATH_MODIFIED)
        return report
    if kind != "semantic_review":
        raise CwError("Semantic review state is inconsistent", ErrorCode.PROTECTED_PATH_MODIFIED)
    decision, criteria, blocking_criteria, issues = validate_reviewer_result(phase, report, root=root)
    if (
        decision.value != report.get("decision")
        or criteria != report.get("criteria")
        or ("blocking_criteria" in report and blocking_criteria != report.get("blocking_criteria"))
        or issues != report.get("blocking_issues")
    ):
        raise CwError("Semantic review decision is inconsistent", ErrorCode.PROTECTED_PATH_MODIFIED)
    index = workflow.index(phase.id)
    expected_status = {
        ReviewDecision.APPROVE: (
            WorkflowState.HUMAN_REVIEW_REQUIRED
            if phase.requires_human_approval
            else (
                WorkflowState.PLANNED_COMPLETE
                if workflow.completion_target is not None
                else WorkflowState.COMPLETED
            )
            if index == len(workflow.phases) - 1
            else WorkflowState.IN_PROGRESS
        ),
        ReviewDecision.REVISE: WorkflowState.REVISION_REQUIRED,
        ReviewDecision.HUMAN_REVIEW_REQUIRED: WorkflowState.HUMAN_REVIEW_REQUIRED,
    }[decision]
    final = index == len(workflow.phases) - 1
    expected_phase = None if final else workflow.phases[index + 1].id
    advanced = decision is ReviewDecision.APPROVE and not phase.requires_human_approval
    attempt_matches = (
        state.get("attempt") == 0
        if advanced
        else report["attempt"] == state.get("attempt")
    )
    phase_matches = not advanced or state.get("current_phase") == expected_phase
    if (
        status is not expected_status
        or not attempt_matches
        or not phase_matches
        or report.get("artifact_hashes") != artifact_hashes(root, phase.artifacts)
    ):
        raise CwError("Semantic review evidence does not match repository state", ErrorCode.PROTECTED_PATH_MODIFIED)
    return report


def _validate_state_evolution(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    workflow: Workflow,
    phase: Phase,
    reviews: list[str],
    gates: list[str],
    report: dict[str, Any] | None,
) -> None:
    if before is None:
        return
    if not reviews:
        if after != before:
            raise CwError("Protected workflow state changed without a review", ErrorCode.PROTECTED_PATH_MODIFIED)
        return
    immutable = {
        "schema_version", "cw_version", "workflow_id", "workflow_version",
        "workflow_sha256", "pending_goal",
    }
    if set(after) != set(before) or any(after.get(key) != before.get(key) for key in immutable):
        raise CwError("Protected workflow state identity changed", ErrorCode.PROTECTED_PATH_MODIFIED)
    if before.get("status") != WorkflowState.IN_PROGRESS.value:
        raise CwError("Protected workflow state had an invalid implementation baseline", ErrorCode.PROTECTED_PATH_MODIFIED)
    prior_history = before.get("history")
    history = after.get("history")
    if not isinstance(prior_history, list) or not isinstance(history, list) or history[:len(prior_history)] != prior_history:
        raise CwError("Protected workflow history was rewritten", ErrorCode.PROTECTED_PATH_MODIFIED)
    # The review validator checks the new record. Here only the state delta is
    # admitted; all pre-existing state and history must remain byte-for-byte equivalent.
    if after.get("last_review") != reviews[0]:
        raise CwError("Protected workflow state points to an unexpected review", ErrorCode.PROTECTED_PATH_MODIFIED)
    if gates:
        if after.get("last_gate") != gates[0]:
            raise CwError("Protected workflow state points to an unexpected gate", ErrorCode.PROTECTED_PATH_MODIFIED)
    elif after.get("last_gate") != before.get("last_gate"):
        raise CwError("Protected workflow state changed its prior gate", ErrorCode.PROTECTED_PATH_MODIFIED)
    status = WorkflowState(str(after.get("status")))
    if status is WorkflowState.ERROR:
        if after.get("attempt") != before.get("attempt") or len(history) != len(prior_history) + 1:
            raise CwError("Infrastructure failure consumed or rewrote review history", ErrorCode.PROTECTED_PATH_MODIFIED)
        event = history[-1]
        metadata = after.get("infrastructure_error")
        error_code = report.get("error_code") if report else None
        if (
            not isinstance(event, dict)
            or event.get("phase") != phase.id
            or event.get("action") != "infrastructure_error"
            or event.get("operation") != "review"
            or event.get("error_code") != error_code
            or not isinstance(metadata, dict)
            or metadata.get("error_code") != error_code
            or metadata.get("retryable") is not True
            or metadata.get("operation") != "review"
            or metadata.get("phase") != phase.id
        ):
            raise CwError("Infrastructure review history event is invalid", ErrorCode.PROTECTED_PATH_MODIFIED)
    else:
        decision = report.get("decision") if report else None
        semantic_attempt = int(before.get("attempt", 0)) + 1
        approved_and_advanced = decision == ReviewDecision.APPROVE.value and not phase.requires_human_approval
        if approved_and_advanced:
            index = workflow.index(phase.id)
            final = index == len(workflow.phases) - 1
            expected_phase = None if final else workflow.phases[index + 1].id
            valid_position = after.get("current_phase") == expected_phase
            valid_attempt = after.get("attempt") == 0
        else:
            valid_position = after.get("current_phase") == before.get("current_phase")
            valid_attempt = after.get("attempt") == semantic_attempt
        if not valid_position or not valid_attempt or len(history) != len(prior_history) + 1:
            raise CwError("Semantic review state delta is invalid", ErrorCode.PROTECTED_PATH_MODIFIED)
        event = history[-1]
        if (
            not isinstance(event, dict)
            or event.get("phase") != phase.id
            or event.get("attempt") != semantic_attempt
        ):
            raise CwError("Semantic review history event is invalid", ErrorCode.PROTECTED_PATH_MODIFIED)
        expected_action = (
            "human_review_required"
            if decision == ReviewDecision.HUMAN_REVIEW_REQUIRED.value or phase.requires_human_approval
            else "approved" if decision == ReviewDecision.APPROVE.value
            else "revision_required"
        )
        if (
            event.get("action") != expected_action
            or after.get("last_error") is not None
            or after.get("infrastructure_error") is not None
        ):
            raise CwError("Semantic review history does not match its decision", ErrorCode.PROTECTED_PATH_MODIFIED)


def verify_protected_paths(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    before: ProtectedSnapshot,
) -> None:
    if before.phase_contract is not None and before.phase_contract != phase_contract_fingerprint(workflow, phase):
        raise CwError("Phase contract changed during implementation", ErrorCode.PROTECTED_PATH_MODIFIED)
    try:
        after = snapshot_protected_paths(root, before.roots)
    except CwError as exc:
        if exc.code in {ErrorCode.USAGE_ERROR, ErrorCode.PROTECTED_PATH_MODIFIED}:
            raise
        raise CwError(
            "Protected workflow metadata became unreadable",
            ErrorCode.PROTECTED_PATH_MODIFIED,
            "Run: cw error, then cw repair --reopen <phase>",
            details=f"{exc.code.value}: {exc.message}\n{exc.details or ''}".rstrip(),
        ) from exc
    state_changed = before.entries.get(".cw/state.json") != after.entries.get(".cw/state.json")
    changed = sorted(
        path for path, digest in before.entries.items()
        if path != ".cw/state.json" and after.entries.get(path) != digest
    )
    if changed:
        raise CwError(
            "Protected workflow metadata changed during implementation",
            ErrorCode.PROTECTED_PATH_MODIFIED,
            "Run: cw error, then cw repair --reopen <phase>",
            details="Changed or removed: " + ", ".join(changed),
        )
    additions = sorted(set(after.entries) - set(before.entries))
    files = [path for path in additions if not path.endswith("/")]
    unexpected = [path for path in additions if path.endswith("/") or not (_is_new_review(path) or _is_new_gate(path))]
    if unexpected:
        raise CwError(
            "Protected workflow metadata was created during implementation",
            ErrorCode.PROTECTED_PATH_MODIFIED,
            "Run: cw error, then inspect the protected paths.",
            details="Created: " + ", ".join(unexpected),
        )
    state = load_state(root)
    reviews = [path for path in files if _is_new_review(path)]
    gates = [path for path in files if _is_new_gate(path)]
    if len(reviews) > 1 or len(gates) > 1:
        raise CwError("Multiple protected review results were created", ErrorCode.PROTECTED_PATH_MODIFIED)
    report = _validate_review(root, workflow, phase, state, reviews[0]) if reviews else None
    if gates:
        if gates[0] != state.get("last_gate"):
            raise CwError("Protected gate was not recorded in workflow state", ErrorCode.PROTECTED_PATH_MODIFIED)
        validate_gate(root, workflow, phase.id)
    if gates and not reviews:
        raise CwError("Approval gate was created without a new independent review", ErrorCode.PROTECTED_PATH_MODIFIED)
    if before.state is not None:
        if not state_changed and reviews:
            raise CwError("Review metadata was created without a state transition", ErrorCode.PROTECTED_PATH_MODIFIED)
        _validate_state_evolution(before.state, state, workflow, phase, reviews, gates, report)
