from __future__ import annotations

import getpass
import json
import re
import secrets
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from cw import __version__
from cw.adapters.structured_output import codex_schema

from .diagnostics import redact, state_error
from .errors import CwError, ErrorCode
from .gates import gate_path, validate_gate
from .models import (
    CompletionContract,
    CompletionDecision,
    CompletionResultStatus,
    Phase,
    Workflow,
    WorkflowState,
)
from .recovery import mark_infrastructure_error
from .schema import SCHEMA_VERSION, schema_version
from .severity import CriterionSeverity
from .state import save_state, transition
from .utils import (
    atomic_json_new,
    load_json,
    safe_project_path,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .workflow import _read_document, load_workflow, validate_workflow, workflow_hash, write_workflow


class CompletionBackend(Protocol):
    def run_completion_reviewer(self, root: Path, prompt: str, schema: Path, timeout: int) -> Any: ...
    def run_extension_planner(self, root: Path, prompt: str, schema: Path, timeout: int) -> Any: ...


RESULT_FIELDS = {"id", "status", "evidence", "rationale"}
FINDING_FIELDS = {"category", "severity", "summary", "evidence", "requirement_ids"}
RECOMMENDATION_FIELDS = {"rationale", "requirement_ids"}
REVIEW_FIELDS = {
    "decision", "contract_results", "system_findings", "missing_evidence",
    "extension_recommendation", "summary",
}
PHASE_FIELDS = {
    "id", "name", "objective", "depends_on", "artifacts", "review_paths",
    "required_commands", "acceptance_criteria", "blocking_criteria",
    "requires_human_approval", "expected_evidence", "completion_requirements",
}


def completion_root(root: Path) -> Path:
    return root / ".cw" / "completion"


def _directory(root: Path, name: str) -> Path:
    path = completion_root(root) / name
    if path.is_symlink():
        raise CwError(f".cw/completion/{name} cannot be a symlink", ErrorCode.SCHEMA_VALIDATION_ERROR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def completion_gate_path(root: Path) -> Path:
    return completion_root(root) / "completion.satisfied.json"


def contract_payload(contract: CompletionContract) -> dict[str, Any]:
    return {
        "id": contract.id,
        "name": contract.name,
        "description": contract.description,
        "target_type": contract.target_type,
        "requirements": [
            {
                "id": item.id,
                "description": item.description,
                "severity": item.severity.value,
                "evidence_expectations": list(item.evidence_expectations),
                "project_specific": item.project_specific,
            }
            for item in contract.requirements
        ],
    }


def contract_hash(contract: CompletionContract) -> str:
    encoded = json.dumps(contract_payload(contract), sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(encoded)


def repository_snapshot(root: Path) -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False,
    )
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=False,
    )
    if files.returncode:
        raise CwError("Could not identify the completion snapshot", ErrorCode.WORKFLOW_ERROR)
    entries: list[tuple[str, str]] = []
    for raw in files.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if relative.startswith((".cw/", ".codex/", ".git/")):
            continue
        path = root / relative
        if path.is_symlink():
            digest = sha256_bytes(("symlink:" + path.readlink().as_posix()).encode())
        elif path.is_file():
            digest = sha256_file(path)
        else:
            continue
        entries.append((relative, digest))
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "git_commit": commit.stdout.strip() or None,
        "source_tree_sha256": sha256_bytes(encoded),
        "file_count": len(entries),
    }


def _reference_path(value: str) -> str | None:
    candidate = value.strip().split(maxsplit=1)[0]
    candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
    return candidate if candidate else None


def _valid_evidence(root: Path, values: Any, *, allow_missing_label: bool = False) -> bool:
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item.strip() for item in values):
        return False
    for value in values:
        if allow_missing_label and value.startswith(("MISSING:", "NOT VERIFIED:")):
            continue
        reference = _reference_path(value)
        if reference is None:
            return False
        try:
            path = safe_project_path(root, reference, must_exist=True)
        except CwError:
            return False
        if not path.is_file() or path.is_symlink():
            return False
    return True


def validate_completion_result(
    root: Path,
    contract: CompletionContract,
    payload: Any,
) -> tuple[CompletionDecision, list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != REVIEW_FIELDS:
        raise CwError("Completion reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    try:
        decision = CompletionDecision(str(payload["decision"]))
    except ValueError as exc:
        raise CwError("Completion reviewer decision is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc
    if not isinstance(payload["summary"], str) or not payload["summary"].strip():
        raise CwError("Completion reviewer summary is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    results = payload["contract_results"]
    if not isinstance(results, list) or len(results) != len(contract.requirements):
        raise CwError("Completion reviewer must evaluate every contract requirement", ErrorCode.SCHEMA_VALIDATION_ERROR)
    configured = {item.id: item for item in contract.requirements}
    received: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict) or set(result) != RESULT_FIELDS:
            raise CwError("Completion contract result is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        requirement_id = result.get("id")
        try:
            status = CompletionResultStatus(str(result.get("status")))
        except ValueError as exc:
            raise CwError("Completion contract result status is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc
        if (
            requirement_id not in configured
            or requirement_id in received
            or not isinstance(result.get("rationale"), str)
            or not result["rationale"].strip()
            or not _valid_evidence(
                root, result.get("evidence"),
                allow_missing_label=status in {CompletionResultStatus.NOT_VERIFIED, CompletionResultStatus.MISSING},
            )
        ):
            raise CwError("Completion contract result evidence is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        received[str(requirement_id)] = result
    findings = payload["system_findings"]
    if not isinstance(findings, list):
        raise CwError("Completion system findings are invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != FINDING_FIELDS
            or finding.get("severity") not in {"blocking", "advisory"}
            or not all(isinstance(finding.get(key), str) and finding[key].strip() for key in ("category", "summary"))
            or not _valid_evidence(root, finding.get("evidence"), allow_missing_label=True)
            or not isinstance(finding.get("requirement_ids"), list)
            or any(item not in configured for item in finding["requirement_ids"])
        ):
            raise CwError("Completion system finding is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    missing = payload["missing_evidence"]
    if not isinstance(missing, list) or not all(isinstance(item, str) and item.strip() for item in missing):
        raise CwError("Completion missing evidence is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    recommendation = payload["extension_recommendation"]
    if (
        not isinstance(recommendation, dict)
        or set(recommendation) != RECOMMENDATION_FIELDS
        or not isinstance(recommendation["rationale"], str)
        or not isinstance(recommendation["requirement_ids"], list)
        or any(item not in configured for item in recommendation["requirement_ids"])
    ):
        raise CwError("Completion extension recommendation is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    unsatisfied = {
        item.id for item in contract.requirements
        if item.severity is CriterionSeverity.BLOCKING
        and received[item.id]["status"] != CompletionResultStatus.VERIFIED.value
    }
    if decision is CompletionDecision.SATISFIED and (unsatisfied or missing):
        raise CwError("SATISFIED requires verified blocking requirements and no missing evidence", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if decision is CompletionDecision.EXTENSION_REQUIRED:
        linked = set(recommendation["requirement_ids"])
        if not unsatisfied or not unsatisfied.issubset(linked) or not recommendation["rationale"].strip():
            raise CwError("Extension recommendation does not close the contract gap", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return decision, results, findings, missing, recommendation


def _semantic_review_payload(review: dict[str, Any]) -> dict[str, Any]:
    return {key: review.get(key) for key in REVIEW_FIELDS}


def completion_reviewer_prompt(root: Path, workflow: Workflow, cycle: int) -> str:
    assert workflow.completion_target is not None
    phase_evidence = [
        {
            "phase": phase.id,
            "name": phase.name,
            "gate": gate_path(root, phase.id).relative_to(root).as_posix(),
            "review_paths": list(phase.review_paths),
        }
        for phase in workflow.phases
    ]
    previous = latest_completion_review(root)
    previous_summary = None
    if previous is not None:
        previous_summary = {
            "cycle": previous.get("cycle"), "decision": previous.get("decision"),
            "contract_results": previous.get("contract_results"),
            "summary": previous.get("summary"),
        }
    return f"""You are the independent CW completion reviewer. Remain strictly read-only.
Evaluate system composition against the declared Completion Contract, not the
quality of isolated phases. Inspect cross-phase assumptions, end-to-end failure
modes, integration and production wiring, concurrency, crash recovery, security
boundaries, data integrity, installation/runtime readiness, operations, and any
contract-specific evidence that applies to this project and target.

Completion cycle: {cycle}
Declared goal: {workflow.goal or ''}
Completion Contract: {json.dumps(contract_payload(workflow.completion_target), ensure_ascii=False)}
Validated phase evidence: {json.dumps(phase_evidence, ensure_ascii=False)}
Previous completion review summary: {json.dumps(previous_summary, ensure_ascii=False)}

Use VERIFIED only for concrete evidence. Use INFERRED when evidence supports a
conclusion indirectly, NOT_VERIFIED when proof is insufficient, and MISSING when
expected evidence is absent. Evidence citations must begin with an existing
project-relative file path (optionally followed by :line). For missing evidence,
use `MISSING: ...` or `NOT VERIFIED: ...`. Never include secrets, credentials,
environment values, or raw private logs.

Do not modify files, invalidate prior gates, approve remediation, or invent
evidence. EXTENSION_REQUIRED is a product/contract gap. BLOCKED means the review
cannot determine the result from available evidence/configuration; it is not a
product failure. Recommend only the smallest coherent scope needed. Return only
the JSON object required by the supplied schema.
"""


def _persist_new(directory: Path, prefix: str, payload: dict[str, Any]) -> Path:
    stamp = str(payload.get("created_at") or utc_now()).replace(":", "").replace("-", "")
    for _ in range(10):
        path = directory / f"{prefix}-{stamp}-{secrets.token_hex(8)}.json"
        try:
            atomic_json_new(path, payload)
        except FileExistsError:
            continue
        return path
    raise CwError("Could not allocate append-only completion evidence", ErrorCode.WORKFLOW_ERROR)


def _redacted_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redacted_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_payload(item) for item in value]
    if isinstance(value, str):
        return redact(value) or ""
    return value


def latest_completion_review(root: Path) -> dict[str, Any] | None:
    directory = completion_root(root) / "reviews"
    if not directory.is_dir() or directory.is_symlink():
        return None
    reviews: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        value = load_json(path)
        if isinstance(value, dict) and value.get("kind") == "completion_review":
            reviews.append(value)
    return max(reviews, key=lambda item: (int(item.get("cycle", 0)), str(item.get("created_at", "")))) if reviews else None


def derive_completion_status(
    root: Path, workflow: Workflow, state: dict[str, Any],
) -> tuple[WorkflowState, str | None]:
    """Derive contract completion position from append-only evidence."""
    try:
        validate_completion_gate(root, workflow)
        return WorkflowState.COMPLETED, None
    except CwError:
        pass
    proposals = completion_root(root) / "proposals"
    authorizations = completion_root(root) / "authorizations"
    decided: set[str] = set()
    if authorizations.is_dir() and not authorizations.is_symlink():
        for path in sorted(authorizations.glob("*.json")):
            value = load_json(path)
            if isinstance(value, dict) and isinstance(value.get("proposal_reference"), str):
                decided.add(value["proposal_reference"])
    if proposals.is_dir() and not proposals.is_symlink():
        candidates: list[tuple[int, str, str]] = []
        for path in sorted(proposals.glob("*.json")):
            reference = path.relative_to(root).as_posix()
            if reference in decided:
                continue
            value = load_json(path)
            if (
                isinstance(value, dict) and value.get("kind") == "extension_proposal"
                and value.get("workflow") == workflow.id
                and value.get("base_workflow_sha256") == workflow_hash(root / ".codex/workflow/phases.yaml")
            ):
                candidates.append((int(value.get("cycle", 0)), str(value.get("created_at", "")), reference))
        if candidates:
            return WorkflowState.EXTENSION_PROPOSED, max(candidates)[2]
    latest = latest_completion_review(root)
    if latest is not None and latest.get("decision") == CompletionDecision.BLOCKED.value:
        return WorkflowState.COMPLETION_BLOCKED, None
    if state.get("status") == WorkflowState.COMPLETION_REVIEW.value:
        return WorkflowState.COMPLETION_REVIEW, None
    return WorkflowState.PLANNED_COMPLETE, None


def create_completion_gate(
    root: Path, workflow: Workflow, review_reference: str, snapshot: dict[str, Any], cycle: int,
) -> Path:
    contract = workflow.completion_target
    if contract is None:
        raise CwError("Legacy workflows do not use completion evidence", ErrorCode.INVALID_STATE)
    review = load_json(safe_project_path(root, review_reference, must_exist=True))
    decision, *_ = validate_completion_result(root, contract, _semantic_review_payload(review))
    if decision is not CompletionDecision.SATISFIED or review.get("snapshot") != snapshot:
        raise CwError("Completion evidence requires a SATISFIED review of this snapshot", ErrorCode.INVALID_GATE)
    phase_gates = [
        {"phase": phase.id, "reference": gate_path(root, phase.id).relative_to(root).as_posix(), "sha256": sha256_file(gate_path(root, phase.id))}
        for phase in workflow.phases
        if validate_gate(root, workflow, phase.id)
    ]
    payload = {
        "schema_version": SCHEMA_VERSION, "cw_version": __version__, "workflow": workflow.id,
        "target": contract.id, "contract_sha256": contract_hash(contract), "cycle": cycle,
        "review_reference": review_reference, "snapshot": snapshot,
        "phase_gates": phase_gates, "satisfied_at": utc_now(),
    }
    path = completion_gate_path(root)
    if path.exists():
        raise CwError("Completion evidence already exists", ErrorCode.INVALID_GATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_new(path, payload)
    return path


def validate_completion_gate(root: Path, workflow: Workflow) -> dict[str, Any]:
    contract = workflow.completion_target
    path = completion_gate_path(root)
    if contract is None or not path.is_file() or path.is_symlink():
        raise CwError("Completion evidence is missing", ErrorCode.INVALID_GATE)
    data = load_json(path)
    schema_version(data, "Completion evidence")
    required = {
        "schema_version", "cw_version", "workflow", "target", "contract_sha256", "cycle",
        "review_reference", "snapshot", "phase_gates", "satisfied_at",
    }
    if (
        not isinstance(data, dict) or set(data) != required
        or data.get("workflow") != workflow.id or data.get("target") != contract.id
        or data.get("contract_sha256") != contract_hash(contract)
        or not isinstance(data.get("cycle"), int) or data["cycle"] < 1
        or not isinstance(data.get("satisfied_at"), str) or not data["satisfied_at"]
    ):
        raise CwError("Completion evidence is invalid", ErrorCode.INVALID_GATE)
    reference = data.get("review_reference")
    if not isinstance(reference, str) or not reference.startswith(".cw/completion/reviews/"):
        raise CwError("Completion review reference is invalid", ErrorCode.INVALID_GATE)
    review = load_json(safe_project_path(root, reference, must_exist=True))
    decision, *_ = validate_completion_result(root, contract, _semantic_review_payload(review))
    if decision is not CompletionDecision.SATISFIED or review.get("snapshot") != data.get("snapshot"):
        raise CwError("Completion review evidence is invalid", ErrorCode.INVALID_GATE)
    expected_gates = []
    for phase in workflow.phases:
        validate_gate(root, workflow, phase.id)
        path_for_phase = gate_path(root, phase.id)
        expected_gates.append({
            "phase": phase.id, "reference": path_for_phase.relative_to(root).as_posix(),
            "sha256": sha256_file(path_for_phase),
        })
    if data.get("phase_gates") != expected_gates or data.get("snapshot") != repository_snapshot(root):
        raise CwError("Completion evidence no longer matches the repository", ErrorCode.INVALID_GATE)
    return data


def extension_planner_prompt(workflow: Workflow, review: dict[str, Any]) -> str:
    assert workflow.completion_target is not None
    return f"""You are the CW extension planner. Remain strictly read-only.
Convert the unsatisfied completion requirements into the smallest coherent set
of independently reviewable phases. Do not create one phase per finding. Never
modify prior phases or claim approval. New phase IDs must continue after the
current final phase and dependencies may reference prior or newly proposed
phases. Every phase must declare expected evidence and the contract requirement
IDs it closes. Never target .git, .codex, or .cw.

Goal: {workflow.goal or ''}
Completion Contract: {json.dumps(contract_payload(workflow.completion_target), ensure_ascii=False)}
Existing phase IDs: {json.dumps([phase.id for phase in workflow.phases])}
Completion review: {json.dumps({key: review[key] for key in REVIEW_FIELDS}, ensure_ascii=False)}

Return only the JSON object required by the supplied schema.
"""


def _validate_extension_phases(root: Path, workflow: Workflow, phases: Any, required_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(phases, list) or not phases or len(phases) > 50:
        raise CwError("Extension planner must return a bounded non-empty phase set", ErrorCode.PLANNER_SCHEMA_ERROR)
    normalized: list[dict[str, Any]] = []
    prior_number = max(int(phase.id.split("-", 1)[0]) for phase in workflow.phases)
    for offset, raw in enumerate(phases, start=1):
        if not isinstance(raw, dict) or set(raw) != PHASE_FIELDS:
            raise CwError("Extension planner phase fields are invalid", ErrorCode.PLANNER_SCHEMA_ERROR)
        try:
            phase_number = int(str(raw.get("id", "0")).split("-", 1)[0])
        except ValueError as exc:
            raise CwError(
                "Extension phase IDs must start with a number",
                ErrorCode.PLANNER_SCHEMA_ERROR,
            ) from exc
        if phase_number != prior_number + offset:
            raise CwError("Extension phase IDs must be contiguous", ErrorCode.PLANNER_SCHEMA_ERROR)
        links = raw.get("completion_requirements")
        if not isinstance(links, list) or not links or any(item not in required_ids for item in links):
            raise CwError("Extension phase requirement links are invalid", ErrorCode.PLANNER_SCHEMA_ERROR)
        if not isinstance(raw.get("expected_evidence"), list) or not raw["expected_evidence"]:
            raise CwError("Extension phase expected evidence is required", ErrorCode.PLANNER_SCHEMA_ERROR)
        normalized.append(dict(raw))
    candidate = replace(workflow, phases=(*workflow.phases, *(Phase.from_dict(item) for item in normalized)))
    validate_workflow(root, candidate)
    closed = {item for phase in normalized for item in phase["completion_requirements"]}
    if not required_ids.issubset(closed):
        raise CwError("Extension phases do not close every recommended requirement", ErrorCode.PLANNER_SCHEMA_ERROR)
    return normalized


def _proposal_reference(root: Path, proposal: dict[str, Any]) -> Path:
    return _persist_new(_directory(root, "proposals"), f"cycle-{proposal['cycle']:02d}", proposal)


def run_completion_review(
    root: Path, workflow: Workflow, state: dict[str, Any], backend: CompletionBackend,
) -> dict[str, Any]:
    contract = workflow.completion_target
    if contract is None:
        raise CwError("This workflow uses legacy completion semantics", ErrorCode.INVALID_STATE)
    from .progress import valid_gate_ids
    if len(valid_gate_ids(root, workflow)) != len(workflow.phases):
        raise CwError("All authorized phase gates must be valid before completion review", ErrorCode.INVALID_GATE)
    current = WorkflowState(str(state["status"]))
    if current not in {WorkflowState.PLANNED_COMPLETE, WorkflowState.COMPLETION_BLOCKED}:
        raise CwError("Completion review is not pending", ErrorCode.INVALID_STATE)
    transition(root, state, WorkflowState.COMPLETION_REVIEW)
    cycle = int(state.get("completion_cycle", 0)) + 1
    snapshot = repository_snapshot(root)
    try:
        response = backend.run_completion_reviewer(
            root, completion_reviewer_prompt(root, workflow, cycle),
            codex_schema("completion-review-output.schema.json"), workflow.review_timeout,
        )
        decision, results, findings, missing, recommendation = validate_completion_result(
            root, contract, response.payload,
        )
        if repository_snapshot(root) != snapshot:
            raise CwError("Repository changed during completion review", ErrorCode.REVIEWER_PROCESS_ERROR)
    except CwError as exc:
        state["last_error"] = state_error(exc)
        mark_infrastructure_error(state, exc, operation="completion_review", phase=None)
        transition(root, state, WorkflowState.COMPLETION_BLOCKED)
        report = {
            "schema_version": SCHEMA_VERSION, "workflow": workflow.id, "target": contract.id,
            "cycle": cycle, "kind": "infrastructure_error", "operation": "completion_review",
            "error_code": exc.code.value, "error": redact(exc.message), "details": redact(exc.details),
            "created_at": utc_now(),
        }
        path = _persist_new(_directory(root, "reviews"), f"cycle-{cycle:02d}-infrastructure", report)
        state["last_completion_review"] = path.relative_to(root).as_posix()
        save_state(root, state)
        raise
    report = {
        "schema_version": SCHEMA_VERSION, "workflow": workflow.id, "target": contract.id,
        "cycle": cycle, "kind": "completion_review", "decision": decision.value,
        "contract_results": results, "system_findings": findings, "missing_evidence": missing,
        "extension_recommendation": recommendation, "summary": response.payload["summary"],
        "contract_sha256": contract_hash(contract), "snapshot": snapshot, "created_at": utc_now(),
    }
    report = _redacted_payload(report)
    review_path = _persist_new(_directory(root, "reviews"), f"cycle-{cycle:02d}", report)
    review_reference = review_path.relative_to(root).as_posix()
    state.update({
        "completion_cycle": cycle, "last_completion_review": review_reference,
        "last_error": None, "infrastructure_error": None,
    })
    state.setdefault("history", []).append({
        "timestamp": report["created_at"], "phase": None, "action": "completion_reviewed",
        "cycle": cycle, "decision": decision.value, "review": review_reference,
    })
    if decision is CompletionDecision.SATISFIED:
        gate = create_completion_gate(root, workflow, review_reference, snapshot, cycle)
        state["last_completion_gate"] = gate.relative_to(root).as_posix()
        state["extension_proposal"] = None
        transition(root, state, WorkflowState.COMPLETED)
        return {**report, "completion_gate": state["last_completion_gate"]}
    if decision is CompletionDecision.BLOCKED:
        transition(root, state, WorkflowState.COMPLETION_BLOCKED)
        return report
    try:
        response = backend.run_extension_planner(
            root, extension_planner_prompt(workflow, report),
            codex_schema("extension-plan-output.schema.json"), workflow.review_timeout,
        )
        if not isinstance(response.payload, dict) or set(response.payload) != {"phases"}:
            raise CwError("Extension planner result schema is invalid", ErrorCode.PLANNER_SCHEMA_ERROR)
        required_ids = set(recommendation["requirement_ids"])
        phases = _validate_extension_phases(root, workflow, response.payload["phases"], required_ids)
    except CwError as exc:
        state["last_error"] = state_error(exc)
        mark_infrastructure_error(state, exc, operation="extension_planning", phase=None)
        transition(root, state, WorkflowState.COMPLETION_BLOCKED)
        save_state(root, state)
        raise
    proposal = {
        "schema_version": SCHEMA_VERSION, "workflow": workflow.id, "target": contract.id,
        "cycle": cycle, "kind": "extension_proposal", "review_reference": review_reference,
        "base_workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml"),
        "rationale": recommendation["rationale"], "requirement_ids": recommendation["requirement_ids"],
        "phases": phases, "created_at": utc_now(),
    }
    proposal_path = _proposal_reference(root, proposal)
    proposal_reference = proposal_path.relative_to(root).as_posix()
    state["extension_proposal"] = proposal_reference
    state.setdefault("history", []).append({
        "timestamp": proposal["created_at"], "phase": None, "action": "extension_proposed",
        "cycle": cycle, "proposal": proposal_reference,
    })
    transition(root, state, WorkflowState.EXTENSION_PROPOSED)
    return {**report, "extension_proposal": proposal_reference, "proposed_phases": phases}


def load_extension_proposal(root: Path, state: dict[str, Any], workflow: Workflow) -> dict[str, Any]:
    reference = state.get("extension_proposal")
    if not isinstance(reference, str) or not reference.startswith(".cw/completion/proposals/"):
        raise CwError("No extension proposal is pending", ErrorCode.INVALID_STATE)
    proposal = load_json(safe_project_path(root, reference, must_exist=True))
    required = {
        "schema_version", "workflow", "target", "cycle", "kind", "review_reference",
        "base_workflow_sha256", "rationale", "requirement_ids", "phases", "created_at",
    }
    if (
        not isinstance(proposal, dict) or set(proposal) != required
        or proposal.get("workflow") != workflow.id or proposal.get("kind") != "extension_proposal"
        or proposal.get("base_workflow_sha256") != workflow_hash(root / ".codex/workflow/phases.yaml")
    ):
        raise CwError("Extension proposal is stale or invalid", ErrorCode.INVALID_STATE)
    _validate_extension_phases(root, workflow, proposal.get("phases"), set(proposal.get("requirement_ids", [])))
    return proposal


def authorize_extension(
    root: Path, workflow: Workflow, state: dict[str, Any], *, approve: bool,
) -> dict[str, Any]:
    if WorkflowState(str(state["status"])) is not WorkflowState.EXTENSION_PROPOSED:
        raise CwError("No extension proposal is awaiting authorization", ErrorCode.INVALID_STATE)
    proposal = load_extension_proposal(root, state, workflow)
    proposal_reference = str(state["extension_proposal"])
    decision = "APPROVED" if approve else "REJECTED"
    authorization = {
        "schema_version": SCHEMA_VERSION, "workflow": workflow.id, "target": proposal["target"],
        "cycle": proposal["cycle"], "kind": "extension_authorization", "decision": decision,
        "proposal_reference": proposal_reference, "proposal_sha256": sha256_file(root / proposal_reference),
        "authorized_by": getpass.getuser() or "operator", "authorized_at": utc_now(),
    }
    approval_path = _persist_new(
        _directory(root, "authorizations"), f"cycle-{int(proposal['cycle']):02d}-{decision.lower()}", authorization,
    )
    approval_reference = approval_path.relative_to(root).as_posix()
    if not approve:
        state["extension_proposal"] = None
        state.setdefault("history", []).append({
            "timestamp": authorization["authorized_at"], "phase": None, "action": "extension_rejected",
            "cycle": proposal["cycle"], "proposal": proposal_reference, "authorization": approval_reference,
        })
        transition(root, state, WorkflowState.PLANNED_COMPLETE)
        return authorization
    document = _read_document(root / ".codex/workflow/phases.yaml")
    existing = document.get("phases")
    if not isinstance(existing, list):
        raise CwError("Workflow phases are invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    document["phases"] = [*existing, *proposal["phases"]]
    document.setdefault("workflow", {})["status"] = "APPROVED"
    write_workflow(root / ".codex/workflow/phases.yaml", document)
    extended = load_workflow(root)
    first = extended.phases[len(workflow.phases)]
    state.update({
        "workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml"),
        "current_phase": first.id, "status": WorkflowState.IN_PROGRESS.value,
        "attempt": 0, "last_review": None, "last_error": None,
        "infrastructure_error": None, "extension_proposal": None,
        "last_completion_gate": None,
    })
    state.setdefault("history", []).append({
        "timestamp": authorization["authorized_at"], "phase": first.id, "action": "extension_approved",
        "cycle": proposal["cycle"], "proposal": proposal_reference, "authorization": approval_reference,
        "phases": [item["id"] for item in proposal["phases"]],
    })
    save_state(root, state)
    return {**authorization, "phases": proposal["phases"], "current_phase": first.id}


def audit_completion_history(root: Path, workflow: Workflow) -> dict[str, int]:
    base = completion_root(root)
    if not base.exists():
        return {"completion_reviews": 0, "proposals": 0, "authorizations": 0, "completion_gates": 0}
    if base.is_symlink() or not base.is_dir():
        raise CwError("Completion evidence directory is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    allowed = {"reviews", "proposals", "authorizations", "completion.satisfied.json"}
    if any(item.name not in allowed for item in base.iterdir()):
        raise CwError("Unexpected completion evidence entry", ErrorCode.SCHEMA_VALIDATION_ERROR)
    contract = workflow.completion_target
    counts = {"completion_reviews": 0, "proposals": 0, "authorizations": 0, "completion_gates": 0}
    review_references: set[str] = set()
    directory = base / "reviews"
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise CwError("Completion reviews directory is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise CwError("Unexpected completion review entry", ErrorCode.SCHEMA_VALIDATION_ERROR)
            review = load_json(path)
            schema_version(review, f"Completion review {path.name}")
            if not isinstance(review, dict) or review.get("workflow") != workflow.id:
                raise CwError("Completion review identity is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
            if review.get("kind") == "completion_review":
                if contract is None:
                    raise CwError("Legacy workflow has contract-aware evidence", ErrorCode.SCHEMA_VALIDATION_ERROR)
                validate_completion_result(root, contract, _semantic_review_payload(review))
            elif review.get("kind") != "infrastructure_error":
                raise CwError("Completion review kind is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
            review_references.add(path.relative_to(root).as_posix())
            counts["completion_reviews"] += 1
    proposal_references: set[str] = set()
    directory = base / "proposals"
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise CwError("Completion proposals directory is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise CwError("Unexpected completion proposal entry", ErrorCode.SCHEMA_VALIDATION_ERROR)
            proposal = load_json(path)
            schema_version(proposal, f"Extension proposal {path.name}")
            if (
                not isinstance(proposal, dict) or proposal.get("workflow") != workflow.id
                or proposal.get("review_reference") not in review_references
                or proposal.get("kind") != "extension_proposal"
            ):
                raise CwError("Extension proposal is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
            proposal_references.add(path.relative_to(root).as_posix())
            counts["proposals"] += 1
    directory = base / "authorizations"
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise CwError("Completion authorizations directory is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise CwError("Unexpected completion authorization entry", ErrorCode.SCHEMA_VALIDATION_ERROR)
            approval = load_json(path)
            schema_version(approval, f"Extension authorization {path.name}")
            reference = approval.get("proposal_reference") if isinstance(approval, dict) else None
            if (
                not isinstance(approval, dict) or approval.get("workflow") != workflow.id
                or approval.get("kind") != "extension_authorization"
                or approval.get("decision") not in {"APPROVED", "REJECTED"}
                or reference not in proposal_references
                or approval.get("proposal_sha256") != sha256_file(root / str(reference))
            ):
                raise CwError("Extension authorization is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
            counts["authorizations"] += 1
    if completion_gate_path(root).exists():
        validate_completion_gate(root, workflow)
        counts["completion_gates"] = 1
    return counts


def recover_approved_extension(root: Path, workflow: Workflow, state: dict[str, Any]) -> bool:
    """Finish an append authorized durably before a supervisor interruption."""
    directory = completion_root(root) / "authorizations"
    if not directory.is_dir() or directory.is_symlink():
        return False
    approvals: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        value = load_json(path)
        if isinstance(value, dict) and value.get("decision") == "APPROVED":
            approvals.append(value)
    for approval in sorted(approvals, key=lambda item: (int(item.get("cycle", 0)), str(item.get("authorized_at", "")))):
        reference = approval.get("proposal_reference")
        if not isinstance(reference, str):
            continue
        proposal = load_json(safe_project_path(root, reference, must_exist=True))
        phases = proposal.get("phases") if isinstance(proposal, dict) else None
        if not isinstance(phases, list) or not phases:
            raise CwError("Approved extension proposal is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
        proposed_ids = [str(item.get("id")) for item in phases if isinstance(item, dict)]
        existing_ids = [phase.id for phase in workflow.phases]
        present = [phase_id in existing_ids for phase_id in proposed_ids]
        if all(present):
            continue
        if any(present):
            raise CwError("Approved extension was only partially appended", ErrorCode.INVALID_STATE)
        if proposal.get("base_workflow_sha256") != workflow_hash(root / ".codex/workflow/phases.yaml"):
            raise CwError("Approved extension does not match the current workflow", ErrorCode.INVALID_STATE)
        _validate_extension_phases(root, workflow, phases, set(proposal.get("requirement_ids", [])))
        document = _read_document(root / ".codex/workflow/phases.yaml")
        document["phases"] = [*document.get("phases", []), *phases]
        document.setdefault("workflow", {})["status"] = "APPROVED"
        write_workflow(root / ".codex/workflow/phases.yaml", document)
        extended = load_workflow(root)
        first = extended.phases[len(workflow.phases)]
        state.update({
            "workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml"),
            "current_phase": first.id, "status": WorkflowState.IN_PROGRESS.value,
            "attempt": 0, "last_review": None, "last_error": None,
            "infrastructure_error": None, "extension_proposal": None,
        })
        exists = any(
            isinstance(event, dict) and event.get("action") == "extension_approved"
            and event.get("cycle") == approval.get("cycle")
            for event in state.setdefault("history", [])
        )
        if not exists:
            state["history"].append({
                "timestamp": approval.get("authorized_at") or utc_now(), "phase": first.id,
                "action": "extension_approved", "cycle": approval.get("cycle"),
                "proposal": reference,
                "authorization": next(
                    path.relative_to(root).as_posix() for path in directory.glob("*.json")
                    if load_json(path) == approval
                ),
                "phases": proposed_ids,
            })
        return True
    return False
