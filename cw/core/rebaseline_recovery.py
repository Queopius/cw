from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cw import __version__

from .errors import CwError, ErrorCode
from .gates import gate_path
from .layout import safe_directory, safe_file, validate_tree
from .models import ReviewDecision, Workflow, WorkflowState
from .platform import fsync_directory, process_is_alive
from .progress import valid_gate_prefix
from .reviews import validate_reviewer_result
from .revisions import (
    active_revision,
    canonical_document_hash,
    revision_id,
    supersession_index,
)
from .schema import SCHEMA_VERSION, schema_version
from .state import load_state
from .utils import (
    atomic_json,
    atomic_json_new,
    load_json,
    safe_project_path,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .workflow import (
    _read_document,
    load_workflow,
    workflow_from_document,
    workflow_hash,
)

TRANSACTION = ".cw/runtime/rebaseline-recovery-transaction.json"
REPAIR_RECEIPTS = ".cw/repair-receipts"
RECOVERY_RECEIPTS = ".cw/rebaseline-recoveries"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RECEIPT_ID = re.compile(r"rr-[0-9a-f]{64}")
_REPAIR_RECEIPT_FIELDS = {
    "schema_version", "kind", "phase", "review_reference", "review_sha256",
    "backup", "backup_sha256", "before_state_sha256", "after_status", "after_attempt",
    "after_revision_attempt", "workflow", "workflow_sha256",
    "active_plan_revision", "active_plan_revision_sha256", "review_decision",
    "created_at", "cw_version",
}
_RECOVERY_RECEIPT_FIELDS = {
    "schema_version", "kind", "recovery_id", "operation_id", "correlation_id",
    "created_at", "transition",
    "request", "provenance", "backup", "backup_sha256", "before_state_sha256",
    "after_state_sha256",
}
_RECOVERY_REQUEST_FIELDS = {
    "schema_version", "kind", "workflow", "phase", "review_reference",
    "review_sha256", "workflow_sha256", "state_sha256", "prior_gate_reference",
    "prior_gate_sha256", "reason",
}
_RECOVERY_TRANSITION_FIELDS = {
    "event_id", "active_plan_revision", "active_plan_revision_sha256",
    "previous_status", "resulting_status", "resulting_last_gate",
}
_RECOVERY_PROVENANCE_FIELDS = {
    "kind", "backup", "backup_sha256", "backup_state_sha256", "receipt", "receipt_sha256",
}


FailureInjector = Callable[[str], None]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _document_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")


def _recovery_id(request: dict[str, Any]) -> str:
    return "rr-" + sha256_bytes(_canonical_bytes(request)).removeprefix("sha256:")


def _recovery_event_id(recovery_id: str) -> str:
    identity = {"action": "rebaseline_recovery_applied", "recovery_id": recovery_id}
    return "rre-" + sha256_bytes(_canonical_bytes(identity)).removeprefix("sha256:")


def _recovery_reference(recovery_id: str) -> str:
    return f"{RECOVERY_RECEIPTS}/{recovery_id}.json"


def _recovery_backup_reference(recovery_id: str) -> str:
    return f".cw/backups/rebaseline-recovery-{recovery_id}"


def _revision_identity_for_document(
    root: Path,
    state: dict[str, Any],
    workflow: Workflow,
    document: dict[str, Any],
) -> tuple[str, str]:
    if state.get("active_plan_revision") is not None:
        return active_revision(root, state, workflow)
    return revision_id(document), canonical_document_hash(document)


def _directory_digest(path: Path, label: str) -> str:
    validate_tree(path, label)
    entries: list[dict[str, Any]] = []
    for entry in sorted(path.rglob("*")):
        metadata = entry.lstat()
        relative = entry.relative_to(path).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative + "/", "mode": stat.S_IMODE(metadata.st_mode)})
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            entries.append({
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size": metadata.st_size,
                "sha256": sha256_file(entry),
            })
        else:
            raise CwError(f"{label} contains an unsafe entry", ErrorCode.PLAN_REVISION_INVALID)
    return sha256_bytes(_canonical_bytes(entries))


def _expected_prior_gate(
    root: Path, workflow: Workflow, phase_id: str,
) -> tuple[str | None, str | None]:
    gates = valid_gate_prefix(root, workflow)
    if len(gates) != workflow.index(phase_id):
        raise CwError("Approval gates do not match the active phase", ErrorCode.INVALID_GATE)
    if not gates:
        return None, None
    reference = gate_path(root, gates[-1][0]).relative_to(root).as_posix()
    path = safe_file(root / reference, "Expected prior gate", required=True)
    return reference, sha256_file(path)


def _validate_prior_gate_authority(
    root: Path,
    workflow: Workflow,
    phase_id: str,
    reference: str | None,
    digest: str | None,
) -> None:
    actual_reference, actual_digest = _expected_prior_gate(root, workflow, phase_id)
    if reference != actual_reference or digest != actual_digest:
        raise CwError(
            "Expected prior gate authority does not match live evidence",
            ErrorCode.OPERATION_CONFLICT,
        )


def _create_recovery_backup(root: Path, recovery_id: str, review_reference: str) -> Path:
    """Create the closed backup whose complete inventory is rooted by the request."""
    backups = safe_directory(root / ".cw/backups", ".cw/backups", create=True)
    target = root / _recovery_backup_reference(recovery_id)
    if target.exists() or target.is_symlink():
        raise CwError("Recovery backup identity already exists", ErrorCode.OPERATION_CONFLICT)
    state = safe_file(root / ".cw/state.json", "Recovery source state", required=True)
    workflow = safe_file(
        root / ".codex/workflow/phases.yaml", "Recovery source workflow", required=True,
    )
    review = _safe_regular_file(
        root, review_reference, parent=root / ".cw/reviews", label="Recovery source review",
    )
    gates = safe_directory(root / ".cw/gates", ".cw/gates")
    target.mkdir(mode=0o700)
    shutil.copy2(state, target / "state.json")
    shutil.copy2(workflow, target / "phases.yaml")
    review_target = target / "reviews"
    review_target.mkdir(mode=0o700)
    shutil.copy2(review, review_target / review.name)
    shutil.copytree(gates, target / "gates")
    for entry in target.rglob("*"):
        if entry.is_file():
            with entry.open("rb") as stream:
                os.fsync(stream.fileno())
    for directory in sorted((entry for entry in target.rglob("*") if entry.is_dir()), reverse=True):
        fsync_directory(directory)
    fsync_directory(target)
    fsync_directory(backups)
    return target


def _validate_recovery_backup(
    root: Path,
    backup: Path,
    request: dict[str, Any],
) -> None:
    review_name = Path(str(request["review_reference"])).name
    expected_entries = {
        "state.json",
        "phases.yaml",
        "reviews/",
        f"reviews/{review_name}",
        "gates/",
    }
    live_gates = safe_directory(root / ".cw/gates", ".cw/gates")
    for gate in sorted(live_gates.iterdir()):
        metadata = gate.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise CwError("Recovery gate inventory is unsafe", ErrorCode.INVALID_GATE)
        expected_entries.add(f"gates/{gate.name}")
    observed_entries: set[str] = set()
    for entry in sorted(backup.rglob("*")):
        metadata = entry.lstat()
        relative = entry.relative_to(backup).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            observed_entries.add(relative + "/")
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            observed_entries.add(relative)
        else:
            raise CwError("Recovery backup contains an unsafe entry", ErrorCode.PLAN_REVISION_INVALID)
    backup_gates = safe_directory(backup / "gates", "Recovery backup gates")
    if (
        observed_entries != expected_entries
        or _directory_digest(backup_gates, "Recovery backup gates")
        != _directory_digest(live_gates, "Current recovery gates")
        or sha256_file(safe_file(
            backup / "reviews" / review_name,
            "Recovery backup review",
            required=True,
        )) != request["review_sha256"]
    ):
        raise CwError("Recovery backup inventory changed", ErrorCode.PLAN_REVISION_INVALID)


def _state_sha(root: Path) -> str:
    return sha256_file(safe_file(root / ".cw/state.json", ".cw/state.json", required=True))


def _validate_digest(value: str, label: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise CwError(f"{label} must be a canonical SHA-256", ErrorCode.USAGE_ERROR, exit_code=2)
    return value


def _safe_regular_file(root: Path, reference: str, *, parent: Path, label: str) -> Path:
    if not reference.startswith(parent.relative_to(root).as_posix() + "/"):
        raise CwError(f"{label} reference is outside its namespace", ErrorCode.PLAN_REVISION_INVALID)
    path = safe_project_path(root, reference, must_exist=True)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CwError(f"{label} cannot be inspected", ErrorCode.PLAN_REVISION_INVALID, details=str(exc)) from exc
    if path.parent != parent or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CwError(f"{label} must be a regular non-linked file", ErrorCode.PLAN_REVISION_INVALID)
    return path


def _validate_review(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
    phase_id: str,
    reference: str,
    expected_sha256: str,
) -> tuple[Path, dict[str, Any], str, str]:
    path = _safe_regular_file(
        root, reference, parent=root / ".cw/reviews", label="Recovery review",
    )
    if sha256_file(path) != expected_sha256:
        raise CwError("Recovery review digest changed", ErrorCode.OPERATION_CONFLICT)
    review = load_json(path)
    schema_version(review, "Recovery review")
    if not isinstance(review, dict) or review.get("kind") != "semantic_review":
        raise CwError("Recovery review schema is invalid", ErrorCode.PLAN_REVISION_INVALID)
    if review.get("decision") != ReviewDecision.REVISE.value:
        raise CwError("Recovery requires an explicit REVISE review", ErrorCode.PLAN_REBASELINE_REQUIRED)
    if review.get("phase") != phase_id or review.get("workflow") != workflow.id:
        raise CwError("Recovery review belongs to another workflow or phase", ErrorCode.PLAN_REVISION_INVALID)
    decision, criteria, blocking, issues = validate_reviewer_result(
        workflow.phase(phase_id), review, root=root,
    )
    if (
        decision is not ReviewDecision.REVISE
        or criteria != review.get("criteria")
        or ("blocking_criteria" in review and blocking != review.get("blocking_criteria"))
        or issues != review.get("blocking_issues")
    ):
        raise CwError("Recovery review is internally inconsistent", ErrorCode.PLAN_REVISION_INVALID)
    active_id, active_hash = active_revision(root, state, workflow)
    if review.get("plan_revision_id") is not None and review.get("plan_revision_id") != active_id:
        raise CwError("Recovery review belongs to another plan revision", ErrorCode.PLAN_REVISION_INVALID)
    if (
        review.get("canonical_workflow_sha256") is not None
        and review.get("canonical_workflow_sha256") != active_hash
    ):
        raise CwError("Recovery review belongs to another workflow revision", ErrorCode.PLAN_REVISION_INVALID)
    if reference in supersession_index(root):
        raise CwError("Recovery review is already superseded", ErrorCode.SUPERSESSION_INVALID)
    compatible: list[str] = []
    for candidate in sorted((root / ".cw/reviews").glob("*.json")):
        if candidate == path:
            compatible.append(reference)
            continue
        candidate_reference = candidate.relative_to(root).as_posix()
        candidate_path = _safe_regular_file(
            root, candidate_reference, parent=root / ".cw/reviews", label="Review evidence",
        )
        payload = load_json(candidate_path)
        if (
            isinstance(payload, dict)
            and payload.get("kind") == "semantic_review"
            and payload.get("decision") == ReviewDecision.REVISE.value
            and payload.get("workflow") == workflow.id
            and payload.get("phase") == phase_id
            and payload.get("plan_revision_id") in {None, active_id}
            and payload.get("canonical_workflow_sha256") in {None, active_hash}
            and candidate_reference not in supersession_index(root)
            and payload.get("attempt") == review.get("attempt")
        ):
            compatible.append(candidate_reference)
    if compatible != [reference]:
        raise CwError("Recovery review identity is ambiguous", ErrorCode.PLAN_REVISION_INVALID)
    return path, review, active_id, active_hash


def _reopen_event(state: dict[str, Any], phase_id: str) -> tuple[int, dict[str, Any]]:
    matches = [
        (index, event) for index, event in enumerate(state.get("history", []))
        if isinstance(event, dict) and event.get("phase") == phase_id and event.get("action") == "reopened"
    ]
    if not matches:
        raise CwError("Recovery requires proven repair --reopen provenance", ErrorCode.PLAN_REBASELINE_REQUIRED)
    index, event = matches[-1]
    if any(
        isinstance(later, dict)
        and later.get("phase") == phase_id
        and later.get("action") not in {"rebaseline_recovery_previewed"}
        for later in state.get("history", [])[index + 1 :]
    ):
        raise CwError("Work occurred after repair --reopen", ErrorCode.OPERATION_CONFLICT)
    return index, event


def _canonical_recovery_request(
    workflow_id: str,
    phase_id: str,
    review_reference: str,
    expected_review_sha256: str,
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    prior_gate_reference: str | None,
    prior_gate_sha256: str | None,
    reason: str,
) -> dict[str, Any]:
    """Return the complete human-authorized request used as the trust root."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "rebaseline_recovery_request",
        "workflow": workflow_id,
        "phase": phase_id,
        "review_reference": review_reference,
        "review_sha256": expected_review_sha256,
        "workflow_sha256": expected_workflow_sha256,
        "state_sha256": expected_state_sha256,
        "prior_gate_reference": prior_gate_reference,
        "prior_gate_sha256": prior_gate_sha256,
        "reason": reason.strip(),
    }


def _reconstruct_recovery(
    request: dict[str, Any],
    before_state: dict[str, Any],
    *,
    active_plan_revision: str,
    active_plan_revision_sha256: str,
    provenance: dict[str, Any],
    backup_reference: str,
    backup_sha256: str,
    last_gate: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Purely reconstruct the authorized transition and its complete receipt.

    The request and its CAS values are authoritative. Persisted recovery state,
    history events and receipts are evidence to compare with this projection;
    none of them contributes derived values to it.
    """
    if set(request) != _RECOVERY_REQUEST_FIELDS:
        raise CwError("Recovery request is invalid", ErrorCode.PLAN_REVISION_INVALID)
    recovery_id = _recovery_id(request)
    receipt_reference = _recovery_reference(recovery_id)
    expected_backup = _recovery_backup_reference(recovery_id)
    if backup_reference != expected_backup:
        raise CwError("Recovery backup identity is invalid", ErrorCode.PLAN_REVISION_INVALID)
    _, reopen = _reopen_event(before_state, str(request["phase"]))
    created_at = reopen.get("timestamp")
    if not isinstance(created_at, str) or not created_at:
        raise CwError("Reopen provenance has no canonical timestamp", ErrorCode.INVALID_STATE)
    transition = {
        "event_id": _recovery_event_id(recovery_id),
        "active_plan_revision": active_plan_revision,
        "active_plan_revision_sha256": active_plan_revision_sha256,
        "previous_status": WorkflowState.IN_PROGRESS.value,
        "resulting_status": WorkflowState.REVISION_REQUIRED.value,
        "resulting_last_gate": last_gate,
    }
    after_state = copy.deepcopy(before_state)
    after_state.update({
        "status": WorkflowState.REVISION_REQUIRED.value,
        "attempt": 0,
        "revision_attempt": 0,
        "last_review": request["review_reference"],
        "last_gate": last_gate,
        "last_error": None,
        "infrastructure_error": None,
        "updated_at": created_at,
    })
    after_state.setdefault("history", []).append({
        "timestamp": created_at,
        "phase": request["phase"],
        "action": "rebaseline_recovery_applied",
        "event_id": transition["event_id"],
        "recovery": receipt_reference,
        "operation_id": recovery_id,
        "correlation_id": recovery_id,
        "review": request["review_reference"],
        "review_sha256": request["review_sha256"],
        "backup": backup_reference,
    })
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": "rebaseline_recovery_receipt",
        "recovery_id": recovery_id,
        "operation_id": recovery_id,
        "correlation_id": recovery_id,
        "created_at": created_at,
        "request": copy.deepcopy(request),
        "transition": transition,
        "provenance": copy.deepcopy(provenance),
        "backup": backup_reference,
        "backup_sha256": backup_sha256,
        "before_state_sha256": request["state_sha256"],
        "after_state_sha256": sha256_bytes(_canonical_bytes(after_state)),
    }
    return after_state, receipt


def _prove_reopen(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
    phase_id: str,
    review_reference: str,
    review_sha256: str,
    *,
    require_current_workflow: bool = True,
) -> dict[str, Any]:
    index, event = _reopen_event(state, phase_id)
    backup_reference = event.get("backup")
    if not isinstance(backup_reference, str) or not re.fullmatch(r"\.cw/backups/[A-Za-z0-9._-]+", backup_reference):
        raise CwError("Reopen backup reference is invalid", ErrorCode.PLAN_REBASELINE_REQUIRED)
    backup = safe_project_path(root, backup_reference, must_exist=True)
    if backup.is_symlink() or not backup.is_dir():
        raise CwError("Reopen backup is unsafe", ErrorCode.PLAN_REBASELINE_REQUIRED)
    validate_tree(backup, "Reopen backup")
    before = load_json(safe_file(backup / "state.json", "Reopen backup state", required=True))
    backup_review = safe_file(backup / review_reference.removeprefix(".cw/"), "Reopen backup review", required=True)
    backup_plan = safe_file(backup / "phases.yaml", "Reopen backup workflow", required=True)
    backup_review_metadata = backup_review.lstat()
    if (
        not isinstance(before, dict)
        or before.get("status") != WorkflowState.ERROR.value
        or before.get("current_phase") != phase_id
        or before.get("last_review") != review_reference
        or before.get("workflow_id") != workflow.id
        or before.get("workflow_sha256") != workflow_hash(backup_plan)
        or sha256_file(backup_review) != review_sha256
        or before.get("history") != state.get("history", [])[:index]
        or not stat.S_ISREG(backup_review_metadata.st_mode)
        or backup_review_metadata.st_nlink != 1
    ):
        raise CwError("Reopen backup does not prove the selected review transition", ErrorCode.PLAN_REBASELINE_REQUIRED)
    receipt_reference = event.get("receipt")
    receipt_sha256 = event.get("receipt_sha256")
    if not isinstance(receipt_reference, str) or not isinstance(receipt_sha256, str):
        raise CwError(
            "Recovery requires an independently bound repair --reopen receipt",
            ErrorCode.PLAN_REBASELINE_REQUIRED,
        )
    receipt_path = _safe_regular_file(
        root, receipt_reference, parent=root / REPAIR_RECEIPTS, label="Reopen receipt",
    )
    receipt = load_json(receipt_path)
    backup_document = _read_document(backup_plan)
    active_id, active_hash = _revision_identity_for_document(
        root, state, workflow, backup_document,
    )
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _REPAIR_RECEIPT_FIELDS
        or receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("kind") != "repair_reopen_receipt"
        or sha256_file(receipt_path) != receipt_sha256
        or receipt.get("phase") != phase_id
        or receipt.get("workflow") != workflow.id
        or receipt.get("workflow_sha256") != workflow_hash(
            root / ".codex/workflow/phases.yaml" if require_current_workflow else backup_plan
        )
        or receipt.get("review_reference") != review_reference
        or receipt.get("review_sha256") != review_sha256
        or receipt.get("review_decision") != ReviewDecision.REVISE.value
        or receipt.get("active_plan_revision") != active_id
        or receipt.get("active_plan_revision_sha256") != active_hash
        or receipt.get("backup") != backup_reference
        or receipt.get("backup_sha256") != _directory_digest(backup, "Reopen backup")
        or receipt.get("before_state_sha256") != sha256_file(backup / "state.json")
        or receipt.get("after_status") != WorkflowState.IN_PROGRESS.value
        or receipt.get("after_attempt") != 0
        or receipt.get("after_revision_attempt") != 0
    ):
        raise CwError("Reopen receipt is invalid", ErrorCode.PLAN_REBASELINE_REQUIRED)
    review_revision = load_json(backup_review)
    if not isinstance(review_revision, dict):
        raise CwError("Reopen review identity is invalid", ErrorCode.PLAN_REBASELINE_REQUIRED)
    explicit_revision = review_revision.get("plan_revision_id")
    explicit_workflow_hash = review_revision.get("canonical_workflow_sha256")
    if explicit_revision is not None and explicit_revision != active_id:
        raise CwError("Reopen review revision is invalid", ErrorCode.PLAN_REBASELINE_REQUIRED)
    if explicit_workflow_hash is not None and explicit_workflow_hash != active_hash:
        raise CwError("Reopen review workflow revision is invalid", ErrorCode.PLAN_REBASELINE_REQUIRED)
    return {
        "kind": "repair_reopen_receipt",
        "backup": backup_reference,
        "backup_sha256": _directory_digest(backup, "Reopen backup"),
        "backup_state_sha256": sha256_file(backup / "state.json"),
        "receipt": receipt_reference,
        "receipt_sha256": receipt_sha256,
    }


def _inactive_runtime(
    root: Path, workflow: Workflow, phase_id: str, *, allow_operation_pid: int | None,
    allow_recovery_transaction: bool = False,
) -> None:
    from cw.core.session import readiness_path, session_path
    from cw.execution.runs import load_active_run
    from cw.execution.session import active_batch

    if (
        readiness_path(root).exists()
        or readiness_path(root).is_symlink()
        or session_path(root).exists()
        or session_path(root).is_symlink()
    ):
        raise CwError("Recovery requires no readiness or implementation session", ErrorCode.LOCKED, exit_code=3)
    if load_active_run(root) is not None or active_batch(root) is not None:
        raise CwError("Recovery requires no managed run or batch", ErrorCode.LOCKED, exit_code=3)
    for relative in (".cw/runtime/active-run.json", ".cw/runtime/batch.json"):
        if (root / relative).is_symlink():
            raise CwError("Active runtime namespace is unsafe", ErrorCode.INVALID_STATE)
    operation = root / ".cw/locks/operation.lock"
    if operation.exists():
        if operation.is_symlink() or not operation.is_file():
            raise CwError("CW operation lock is unsafe", ErrorCode.LOCKED, exit_code=3)
        lock = load_json(operation)
        pid = lock.get("pid") if isinstance(lock, dict) else None
        if pid != allow_operation_pid:
            if isinstance(pid, int) and pid > 0 and process_is_alive(pid):
                raise CwError("Another CW operation is active", ErrorCode.LOCKED, exit_code=3)
            raise CwError("A stale operation lock requires repair", ErrorCode.TRANSACTION_RECOVERY_REQUIRED, exit_code=3)
    for relative in (
        ".cw/runtime/plan-rebaseline-transaction.json",
        ".cw/runtime/plan-amend-transaction.json",
    ):
        if (root / relative).exists() or (root / relative).is_symlink():
            raise CwError("A pending CW transaction must be recovered first", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    if not allow_recovery_transaction and ((root / TRANSACTION).exists() or (root / TRANSACTION).is_symlink()):
        raise CwError("A pending CW transaction must be recovered first", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    if gate_path(root, phase_id).exists() or gate_path(root, phase_id).is_symlink():
        raise CwError("The active phase already has an approval gate", ErrorCode.INVALID_GATE)
    for relative in (REPAIR_RECEIPTS, RECOVERY_RECEIPTS):
        namespace = root / relative
        if namespace.exists() or namespace.is_symlink():
            safe_directory(namespace, relative)


def _validate_repair_provenance(root: Path, provenance: dict[str, Any]) -> None:
    if set(provenance) != _RECOVERY_PROVENANCE_FIELDS or provenance.get("kind") != "repair_reopen_receipt":
        raise CwError("Recovery provenance is invalid", ErrorCode.PLAN_REVISION_INVALID)
    reference = provenance.get("receipt")
    expected = provenance.get("receipt_sha256")
    if not isinstance(reference, str) or not isinstance(expected, str):
        raise CwError("Recovery provenance is incomplete", ErrorCode.PLAN_REVISION_INVALID)
    path = _safe_regular_file(
        root, reference, parent=root / REPAIR_RECEIPTS, label="Recovery provenance receipt",
    )
    if sha256_file(path) != expected:
        raise CwError("Recovery provenance receipt changed", ErrorCode.PLAN_REVISION_INVALID)
    payload = load_json(path)
    if not isinstance(payload, dict) or set(payload) != _REPAIR_RECEIPT_FIELDS:
        raise CwError("Recovery provenance receipt is invalid", ErrorCode.PLAN_REVISION_INVALID)
    backup = provenance.get("backup")
    backup_sha = provenance.get("backup_state_sha256")
    backup_tree_sha = provenance.get("backup_sha256")
    if (
        payload.get("backup") != backup
        or payload.get("backup_sha256") != backup_tree_sha
        or payload.get("before_state_sha256") != backup_sha
        or not isinstance(backup, str)
        or not isinstance(backup_sha, str)
        or not isinstance(backup_tree_sha, str)
    ):
        raise CwError("Recovery provenance backup identity changed", ErrorCode.PLAN_REVISION_INVALID)
    backup_path = safe_project_path(root, backup, must_exist=True)
    if backup_path.is_symlink() or not backup_path.is_dir():
        raise CwError("Recovery provenance backup is unsafe", ErrorCode.PLAN_REVISION_INVALID)
    validate_tree(backup_path, "Recovery provenance backup")
    if _directory_digest(backup_path, "Recovery provenance backup") != backup_tree_sha:
        raise CwError("Recovery provenance backup inventory changed", ErrorCode.PLAN_REVISION_INVALID)
    if sha256_file(safe_file(backup_path / "state.json", "Recovery provenance state", required=True)) != backup_sha:
        raise CwError("Recovery provenance backup changed", ErrorCode.PLAN_REVISION_INVALID)


def _validate_recovery_receipt(
    root: Path,
    path: Path,
    payload: Any,
    state: dict[str, Any],
    *,
    expected_request: dict[str, Any] | None = None,
    require_live_state: bool = False,
) -> dict[str, Any]:
    persisted_request = payload.get("request") if isinstance(payload, dict) else None
    request = expected_request if expected_request is not None else persisted_request
    recovery_id = _recovery_id(request) if isinstance(request, dict) else ""
    transition = payload.get("transition") if isinstance(payload, dict) else None
    provenance = payload.get("provenance") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != _RECOVERY_RECEIPT_FIELDS
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != "rebaseline_recovery_receipt"
        or not isinstance(request, dict)
        or set(request) != _RECOVERY_REQUEST_FIELDS
        or persisted_request != request
        or request.get("schema_version") != SCHEMA_VERSION
        or request.get("kind") != "rebaseline_recovery_request"
        or not isinstance(request.get("workflow"), str)
        or not isinstance(request.get("phase"), str)
        or not isinstance(request.get("review_reference"), str)
        or not request["review_reference"].startswith(".cw/reviews/")
        or (request.get("prior_gate_reference") is not None and not isinstance(request.get("prior_gate_reference"), str))
        or (request.get("prior_gate_sha256") is not None and _DIGEST.fullmatch(str(request.get("prior_gate_sha256"))) is None)
        or ((request.get("prior_gate_reference") is None) != (request.get("prior_gate_sha256") is None))
        or not isinstance(request.get("reason"), str)
        or not request["reason"].strip()
        or any(
            not isinstance(request.get(field), str) or _DIGEST.fullmatch(request[field]) is None
            for field in ("review_sha256", "workflow_sha256", "state_sha256")
        )
        or _RECEIPT_ID.fullmatch(recovery_id) is None
        or path.stem != recovery_id
        or path.relative_to(root).as_posix() != _recovery_reference(recovery_id)
        or payload.get("recovery_id") != recovery_id
        or payload.get("operation_id") != recovery_id
        or payload.get("correlation_id") != recovery_id
        or not isinstance(payload.get("created_at"), str)
        or not payload.get("created_at")
        or not isinstance(transition, dict)
        or set(transition) != _RECOVERY_TRANSITION_FIELDS
        or not isinstance(provenance, dict)
        or payload.get("before_state_sha256") != request.get("state_sha256")
        or any(
            not isinstance(payload.get(field), str) or _DIGEST.fullmatch(payload[field]) is None
            for field in ("backup_sha256", "before_state_sha256", "after_state_sha256")
        )
    ):
        raise CwError("Recovery receipt is invalid", ErrorCode.PLAN_REVISION_INVALID)
    backup_reference = _recovery_backup_reference(recovery_id)
    if payload.get("backup") != backup_reference:
        raise CwError("Recovery backup reference changed", ErrorCode.PLAN_REVISION_INVALID)
    backup = safe_project_path(root, backup_reference, must_exist=True)
    if backup.is_symlink() or not backup.is_dir():
        raise CwError("Recovery backup is unsafe", ErrorCode.PLAN_REVISION_INVALID)
    validate_tree(backup, "Recovery backup")
    _validate_recovery_backup(root, backup, request)
    backup_state_path = safe_file(backup / "state.json", "Recovery backup state", required=True)
    if sha256_file(backup_state_path) != request["state_sha256"]:
        raise CwError("Recovery backup state changed", ErrorCode.PLAN_REVISION_INVALID)
    before_state = load_json(backup_state_path)
    if not isinstance(before_state, dict):
        raise CwError("Recovery backup state is invalid", ErrorCode.PLAN_REVISION_INVALID)
    backup_workflow_path = safe_file(
        backup / "phases.yaml", "Recovery backup workflow", required=True,
    )
    if workflow_hash(backup_workflow_path) != request["workflow_sha256"]:
        raise CwError("Recovery backup workflow changed", ErrorCode.PLAN_REVISION_INVALID)
    backup_workflow_document = _read_document(backup_workflow_path)
    workflow = workflow_from_document(root, backup_workflow_document)
    if workflow.id != request["workflow"]:
        raise CwError("Recovery workflow identity changed", ErrorCode.PLAN_REVISION_INVALID)
    # Without an externally supplied request this is only structural evidence.
    # It must never be promoted to an authorized replay or reconstruction.
    if expected_request is None:
        if payload.get("backup_sha256") != _directory_digest(backup, "Recovery backup"):
            raise CwError("Recovery backup digest changed", ErrorCode.PLAN_REVISION_INVALID)
        return payload
    _validate_prior_gate_authority(
        root,
        workflow,
        request["phase"],
        request["prior_gate_reference"],
        request["prior_gate_sha256"],
    )
    if require_live_state:
        live_workflow = load_workflow(root)
        if (
            live_workflow.id != request["workflow"]
            or workflow_hash(root / ".codex/workflow/phases.yaml") != request["workflow_sha256"]
        ):
            raise CwError(
                "Recovery workflow no longer matches its authorization",
                ErrorCode.OPERATION_CONFLICT,
            )
        review_path, _review, active_id, active_hash = _validate_review(
            root,
            live_workflow,
            before_state,
            request["phase"],
            request["review_reference"],
            request["review_sha256"],
        )
    else:
        active_id, active_hash = _revision_identity_for_document(
            root, before_state, workflow, backup_workflow_document,
        )
        review_path = _safe_regular_file(
            root,
            request["review_reference"],
            parent=root / ".cw/reviews",
            label="Recovery review",
        )
    if sha256_file(review_path) != request["review_sha256"]:
        raise CwError("Recovery review no longer matches its authorization", ErrorCode.OPERATION_CONFLICT)
    expected_provenance = _prove_reopen(
        root,
        workflow,
        before_state,
        request["phase"],
        request["review_reference"],
        request["review_sha256"],
        require_current_workflow=require_live_state,
    )
    _validate_repair_provenance(root, expected_provenance)
    last_gate = request["prior_gate_reference"]
    backup_sha256 = _directory_digest(backup, "Recovery backup")
    expected_state, expected_receipt = _reconstruct_recovery(
        request,
        before_state,
        active_plan_revision=active_id,
        active_plan_revision_sha256=active_hash,
        provenance=expected_provenance,
        backup_reference=backup_reference,
        backup_sha256=backup_sha256,
        last_gate=last_gate,
    )
    if (
        provenance != expected_provenance
        or payload != expected_receipt
        or path.read_bytes() != _document_bytes(expected_receipt)
    ):
        raise CwError("Recovery receipt differs from its canonical reconstruction", ErrorCode.INVALID_STATE)
    expected_history = expected_state.get("history")
    live_history = state.get("history")
    if (
        not isinstance(expected_history, list)
        or not isinstance(live_history, list)
        or live_history[: len(expected_history)] != expected_history
    ):
        raise CwError("Recovery history differs from its canonical reconstruction", ErrorCode.INVALID_STATE)
    if require_live_state and state != expected_state:
        raise CwError("Recovery state differs from its canonical reconstruction", ErrorCode.INVALID_STATE)
    return expected_receipt


def _validate_recovery_receipts(root: Path, state: dict[str, Any] | None = None) -> None:
    directory = root / RECOVERY_RECEIPTS
    if not directory.exists() and not directory.is_symlink():
        return
    safe_directory(directory, RECOVERY_RECEIPTS)
    for path in sorted(directory.iterdir()):
        if re.fullmatch(r"rr-[0-9a-f]{64}\.json", path.name) is None:
            raise CwError(
                "Recovery receipt namespace contains an unexpected entry",
                ErrorCode.PLAN_REVISION_INVALID,
            )
        validated_path = _safe_regular_file(
            root, path.relative_to(root).as_posix(), parent=directory,
            label="Recovery receipt",
        )
        payload = load_json(validated_path)
        current_state = state if state is not None else load_state(root)
        _validate_recovery_receipt(root, validated_path, payload, current_state)


def _preflight(
    root: Path,
    phase_id: str,
    review_reference: str,
    expected_review_sha256: str,
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    expected_prior_gate_reference: str | None,
    expected_prior_gate_sha256: str | None,
    reason: str,
    *,
    allow_operation_pid: int | None = None,
    allow_recovery_transaction: bool = False,
) -> dict[str, Any]:
    for value, label in (
        (expected_review_sha256, "Review digest"),
        (expected_workflow_sha256, "Workflow CAS"),
        (expected_state_sha256, "State CAS"),
    ):
        _validate_digest(value, label)
    if not reason.strip():
        raise CwError("Rebaseline recovery requires a reason", ErrorCode.USAGE_ERROR, exit_code=2)
    workflow = load_workflow(root)
    state = load_state(root)
    actual_workflow = workflow_hash(root / ".codex/workflow/phases.yaml")
    actual_state = _state_sha(root)
    if actual_workflow != expected_workflow_sha256:
        raise CwError("Workflow changed before recovery", ErrorCode.STALE_WORKFLOW_SHA)
    if actual_state != expected_state_sha256:
        raise CwError("State changed before recovery", ErrorCode.STALE_STATE_SHA)
    if phase_id != state.get("current_phase"):
        raise CwError("Recovery phase is not the active phase", ErrorCode.INVALID_STATE)
    if state.get("status") != WorkflowState.IN_PROGRESS.value:
        raise CwError("Recovery requires the post-reopen IN_PROGRESS state", ErrorCode.INVALID_STATE)
    if state.get("attempt") != 0:
        raise CwError("Work attempts exist after repair --reopen", ErrorCode.OPERATION_CONFLICT)
    revision_attempt = state.get("revision_attempt", 0)
    if isinstance(revision_attempt, bool) or not isinstance(revision_attempt, int) or revision_attempt != 0:
        raise CwError("Revision attempts exist after repair --reopen", ErrorCode.OPERATION_CONFLICT)
    if state.get("last_review") is not None:
        raise CwError("State already references a different review", ErrorCode.OPERATION_CONFLICT)
    if state.get("last_error") is not None or state.get("infrastructure_error") is not None:
        raise CwError("Post-reopen error state is inconsistent", ErrorCode.INVALID_STATE)
    _inactive_runtime(
        root, workflow, phase_id,
        allow_operation_pid=allow_operation_pid,
        allow_recovery_transaction=allow_recovery_transaction,
    )
    _validate_recovery_receipts(root, state)
    review_path, review, revision_id, revision_sha = _validate_review(
        root, workflow, state, phase_id, review_reference, expected_review_sha256,
    )
    provenance = _prove_reopen(
        root, workflow, state, phase_id, review_reference, expected_review_sha256,
    )
    _validate_prior_gate_authority(
        root, workflow, phase_id, expected_prior_gate_reference, expected_prior_gate_sha256,
    )
    request = _canonical_recovery_request(
        workflow.id,
        phase_id,
        review_reference,
        expected_review_sha256,
        expected_workflow_sha256,
        expected_state_sha256,
        expected_prior_gate_reference,
        expected_prior_gate_sha256,
        reason,
    )
    identifier = _recovery_id(request)
    return {
        "workflow": workflow,
        "state": state,
        "review": review,
        "review_path": review_path,
        "provenance": provenance,
        "last_gate": expected_prior_gate_reference,
        "request": request,
        "recovery_id": identifier,
        "active_plan_revision": revision_id,
        "active_plan_revision_sha256": revision_sha,
        "revision_attempt_before": revision_attempt,
    }


def preview_rebaseline_recovery(
    root: Path,
    phase_id: str,
    review_reference: str,
    expected_review_sha256: str,
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    expected_prior_gate_reference: str | None = None,
    expected_prior_gate_sha256: str | None = None,
) -> dict[str, Any]:
    prepared = _preflight(
        root, phase_id, review_reference, expected_review_sha256,
        expected_workflow_sha256, expected_state_sha256,
        expected_prior_gate_reference, expected_prior_gate_sha256, reason,
    )
    return _result(prepared, applied=False, backup=None, receipt=None)


def _result(
    prepared: dict[str, Any], *, applied: bool, backup: str | None, receipt: str | None,
) -> dict[str, Any]:
    request = prepared["request"]
    return {
        "status": "RECOVERED" if applied else "RECOVERY_PREVIEW",
        "changed": applied,
        "idempotent_replay": False,
        "operation_id": prepared["recovery_id"],
        "recovery_id": prepared["recovery_id"],
        "phase": request["phase"],
        "review_reference": request["review_reference"],
        "review_sha256": request["review_sha256"],
        "workflow_sha256": request["workflow_sha256"],
        "state_sha256": request["state_sha256"],
        "previous_status": WorkflowState.IN_PROGRESS.value,
        "resulting_status": WorkflowState.REVISION_REQUIRED.value,
        "last_gate": prepared.get("last_gate"),
        "backup": backup,
        "recovery_receipt": receipt,
        "provenance": prepared["provenance"]["kind"],
        "next_action": "Create a separate plan rebaseline proposal; its apply requires independent authorization.",
    }


def _replay_result(
    root: Path,
    path: Path,
    payload: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    state = load_state(root)
    validated = _validate_recovery_receipt(
        root, path, payload, state, expected_request=request, require_live_state=True,
    )
    transition = validated["transition"]
    prepared = {
        "request": request,
        "recovery_id": _recovery_id(request),
        "last_gate": transition["resulting_last_gate"],
        "provenance": validated["provenance"],
    }
    result = _result(
        prepared,
        applied=True,
        backup=validated["backup"],
        receipt=path.relative_to(root).as_posix(),
    )
    # The domain is already recovered, but this invocation performs no
    # persistent mutation. Keep the domain result while projecting the
    # invocation-level mutation flag consistently.
    return {**result, "changed": False, "idempotent_replay": True}


def _transaction_path(root: Path) -> Path:
    return root / TRANSACTION


def recover_rebaseline_recovery_transaction(root: Path) -> dict[str, Any] | None:
    path = _transaction_path(root)
    if not path.exists():
        return None
    journal = load_json(safe_file(path, "Rebaseline recovery transaction", required=True))
    required = {
        "schema_version", "kind", "status", "recovery_id", "old_state",
        "backup", "backup_sha256", "receipt", "created_directory",
    }
    recovery_id_value = journal.get("recovery_id") if isinstance(journal, dict) else None
    receipt_value = journal.get("receipt") if isinstance(journal, dict) else None
    if (
        not isinstance(journal, dict)
        or set(journal) != required
        or journal.get("schema_version") != SCHEMA_VERSION
        or journal.get("kind") != "rebaseline_recovery_transaction"
        or journal.get("status") not in {"PREPARED", "BACKUP_READY", "COMMITTED"}
        or not isinstance(recovery_id_value, str)
        or _RECEIPT_ID.fullmatch(recovery_id_value) is None
        or receipt_value != f"{RECOVERY_RECEIPTS}/{recovery_id_value}.json"
        or not isinstance(journal.get("created_directory"), bool)
        or not isinstance(journal.get("backup"), str)
        or (journal.get("backup_sha256") is not None and _DIGEST.fullmatch(str(journal.get("backup_sha256"))) is None)
        or re.fullmatch(r"\.cw/backups/[A-Za-z0-9._-]+", journal["backup"]) is None
    ):
        raise CwError("Rebaseline recovery journal is corrupt", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    backup = safe_project_path(root, journal["backup"], must_exist=False)
    if journal.get("status") == "PREPARED" and not backup.exists():
        path.unlink()
        return {"recovered": True, "recovery_id": journal.get("recovery_id")}
    if backup.is_symlink() or not backup.is_dir():
        raise CwError("Rebaseline recovery backup is unsafe", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    validate_tree(backup, "Rebaseline recovery backup")
    if journal.get("status") == "BACKUP_READY" and journal.get("backup_sha256") != _directory_digest(backup, "Rebaseline recovery backup"):
        raise CwError("Rebaseline recovery backup digest changed", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    if journal.get("status") == "COMMITTED":
        committed_receipt = _safe_regular_file(
            root, journal["receipt"], parent=root / RECOVERY_RECEIPTS,
            label="Committed recovery receipt",
        )
        committed_payload = load_json(committed_receipt)
        try:
            _validate_recovery_receipt(
                root,
                committed_receipt,
                committed_payload,
                load_state(root),
                require_live_state=True,
            )
        except CwError as exc:
            raise CwError(
                "Committed recovery receipt is invalid",
                ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
            ) from exc
        path.unlink()
        return {"recovered": False, "committed": True, "recovery_id": journal.get("recovery_id")}
    old_state = journal.get("old_state")
    receipt = journal.get("receipt")
    if not isinstance(old_state, dict) or not isinstance(receipt, str):
        raise CwError("Rebaseline recovery journal is corrupt", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
    atomic_json(root / ".cw/state.json", old_state)
    receipt_path = safe_project_path(root, receipt)
    if receipt_path.exists():
        metadata = receipt_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise CwError("Recovery receipt target is unsafe", ErrorCode.TRANSACTION_RECOVERY_REQUIRED)
        receipt_path.unlink()
    if journal.get("created_directory") is True:
        directory = root / RECOVERY_RECEIPTS
        if not directory.exists():
            path.unlink()
            return {"recovered": True, "recovery_id": journal.get("recovery_id")}
        try:
            directory.rmdir()
        except OSError as exc:
            raise CwError("Recovery receipt directory is not empty", ErrorCode.TRANSACTION_RECOVERY_REQUIRED, details=str(exc)) from exc
    if backup.exists():
        shutil.rmtree(backup)
        fsync_directory(backup.parent)
    path.unlink()
    return {"recovered": True, "recovery_id": journal.get("recovery_id")}


def apply_rebaseline_recovery(
    root: Path,
    phase_id: str,
    review_reference: str,
    expected_review_sha256: str,
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    expected_prior_gate_reference: str | None = None,
    expected_prior_gate_sha256: str | None = None,
    failure_injector: FailureInjector | None = None,
) -> dict[str, Any]:
    recover_rebaseline_recovery_transaction(root)
    receipt_candidate = root / RECOVERY_RECEIPTS
    workflow_identity = load_workflow(root).id
    exact_request = _canonical_recovery_request(
        workflow_identity,
        phase_id,
        review_reference,
        expected_review_sha256,
        expected_workflow_sha256,
        expected_state_sha256,
        expected_prior_gate_reference,
        expected_prior_gate_sha256,
        reason,
    )
    exact_recovery_id = _recovery_id(exact_request)
    exact_receipt_reference = _recovery_reference(exact_recovery_id)
    exact_receipt_path = root / exact_receipt_reference
    # The human request selects exactly one receipt path. Persisted requests
    # are never searched as authority for an idempotent replay.
    if receipt_candidate.exists() or receipt_candidate.is_symlink():
        current_state = load_state(root)
        _validate_recovery_receipts(root, current_state)
        if exact_receipt_path.exists() or exact_receipt_path.is_symlink():
            validated_path = _safe_regular_file(
                root,
                exact_receipt_reference,
                parent=receipt_candidate,
                label="Recovery receipt",
            )
            payload = load_json(validated_path)
            return _replay_result(root, validated_path, payload, exact_request)
        for path in sorted(receipt_candidate.iterdir()):
            validated_path = _safe_regular_file(
                root, path.relative_to(root).as_posix(), parent=receipt_candidate,
                label="Recovery receipt",
            )
            payload = load_json(validated_path)
            persisted_request = payload.get("request") if isinstance(payload, dict) else None
            if (
                isinstance(persisted_request, dict)
                and persisted_request.get("state_sha256") == expected_state_sha256
            ):
                raise CwError("Recovery state CAS was already consumed", ErrorCode.OPERATION_CONFLICT)
    prepared = _preflight(
        root, phase_id, review_reference, expected_review_sha256,
        expected_workflow_sha256, expected_state_sha256,
        expected_prior_gate_reference, expected_prior_gate_sha256, reason,
        allow_operation_pid=os.getpid(),
    )
    # Revalidate both CAS values after every potentially expensive proof.
    if workflow_hash(root / ".codex/workflow/phases.yaml") != expected_workflow_sha256:
        raise CwError("Workflow changed during recovery", ErrorCode.OPERATION_CONFLICT)
    if _state_sha(root) != expected_state_sha256:
        raise CwError("State changed during recovery", ErrorCode.OPERATION_CONFLICT)
    backup_reference = _recovery_backup_reference(prepared["recovery_id"])
    backup_target = root / backup_reference
    if backup_target.exists() or backup_target.is_symlink():
        raise CwError("Recovery backup identity already exists", ErrorCode.OPERATION_CONFLICT)
    directory_existed = receipt_candidate.exists()
    if directory_existed:
        safe_directory(receipt_candidate, RECOVERY_RECEIPTS)
    receipt_reference = _recovery_reference(prepared["recovery_id"])
    receipt_path = root / receipt_reference
    old_state = copy.deepcopy(prepared["state"])
    journal = {
        "schema_version": SCHEMA_VERSION,
        "kind": "rebaseline_recovery_transaction",
        "status": "PREPARED",
        "recovery_id": prepared["recovery_id"],
        "old_state": old_state,
        "backup": backup_reference,
        "backup_sha256": None,
        "receipt": receipt_reference,
        "created_directory": not directory_existed,
    }
    atomic_json_new(_transaction_path(root), journal)
    step = failure_injector or (lambda _name: None)
    try:
        step("journal_persisted")
        backup = _create_recovery_backup(
            root, prepared["recovery_id"], review_reference,
        )
        step("backup_created")
        backup_sha256 = _directory_digest(backup, "Recovery backup")
        journal["status"] = "BACKUP_READY"
        journal["backup_sha256"] = backup_sha256
        atomic_json(_transaction_path(root), journal)
        step("backup_ready")
        revalidated = _preflight(
            root, phase_id, review_reference, expected_review_sha256,
            expected_workflow_sha256, expected_state_sha256,
            expected_prior_gate_reference, expected_prior_gate_sha256, reason,
            allow_operation_pid=os.getpid(),
            allow_recovery_transaction=True,
        )
        if (
            revalidated["recovery_id"] != prepared["recovery_id"]
            or revalidated["last_gate"] != prepared["last_gate"]
            or revalidated["provenance"] != prepared["provenance"]
        ):
            raise CwError("Recovery evidence changed after backup", ErrorCode.OPERATION_CONFLICT)
        _validate_recovery_backup(root, backup, prepared["request"])
        expected_state, receipt = _reconstruct_recovery(
            prepared["request"],
            old_state,
            active_plan_revision=prepared["active_plan_revision"],
            active_plan_revision_sha256=prepared["active_plan_revision_sha256"],
            provenance=prepared["provenance"],
            backup_reference=backup_reference,
            backup_sha256=backup_sha256,
            last_gate=prepared["last_gate"],
        )
        if not directory_existed:
            try:
                receipt_candidate.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise CwError("Recovery receipt namespace changed during apply", ErrorCode.OPERATION_CONFLICT) from exc
            fsync_directory(receipt_candidate.parent)
        step("receipt_directory_ready")
        atomic_json(root / ".cw/state.json", expected_state)
        step("state_persisted")
        result = _result(
            prepared, applied=True, backup=backup.relative_to(root).as_posix(), receipt=receipt_reference,
        )
        atomic_json_new(receipt_path, receipt)
        step("receipt_persisted")
        journal["status"] = "COMMITTED"
        atomic_json(_transaction_path(root), journal)
        step("committed")
        _transaction_path(root).unlink()
        return result
    except Exception:
        recover_rebaseline_recovery_transaction(root)
        raise


def write_reopen_receipt(
    root: Path,
    *,
    phase_id: str,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
    backup: Path,
) -> tuple[str, str] | None:
    review_reference = before_state.get("last_review")
    if not isinstance(review_reference, str) or not review_reference.startswith(".cw/reviews/"):
        return None
    review = _safe_regular_file(
        root, review_reference, parent=root / ".cw/reviews", label="Reopen review",
    )
    review_payload = load_json(review)
    if not isinstance(review_payload, dict):
        raise CwError("Reopen review is invalid", ErrorCode.PLAN_REVISION_INVALID)
    workflow = load_workflow(root)
    revision_id, revision_sha = active_revision(root, before_state, workflow)
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": "repair_reopen_receipt",
        "phase": phase_id,
        "review_reference": review_reference,
        "review_sha256": sha256_file(review),
        "review_decision": review_payload.get("decision"),
        "workflow": workflow.id,
        "workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml"),
        "active_plan_revision": revision_id,
        "active_plan_revision_sha256": revision_sha,
        "backup": backup.relative_to(root).as_posix(),
        "backup_sha256": _directory_digest(backup, "Reopen backup"),
        "before_state_sha256": sha256_file(backup / "state.json"),
        "after_status": after_state.get("status"),
        "after_attempt": after_state.get("attempt"),
        "after_revision_attempt": after_state.get("revision_attempt"),
        "created_at": utc_now(),
        "cw_version": __version__,
    }
    identifier = "rr-" + sha256_bytes(_canonical_bytes(body)).removeprefix("sha256:")
    directory = safe_directory(root / REPAIR_RECEIPTS, REPAIR_RECEIPTS, create=True)
    path = directory / f"{identifier}.json"
    atomic_json_new(path, body)
    return path.relative_to(root).as_posix(), sha256_file(path)
