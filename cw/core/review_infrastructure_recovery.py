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

from cw.checks.verification import validate_verification_receipt
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import gate_path
from cw.core.layout import validate_tree
from cw.core.models import WorkflowState
from cw.core.platform import fsync_directory
from cw.core.schema import SCHEMA_VERSION
from cw.core.session import session_path
from cw.core.state import load_state
from cw.core.utils import (
    atomic_json,
    atomic_json_new,
    load_json,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from cw.core.workflow import load_workflow, workflow_hash
from cw.execution.runs import load_active_run
from cw.execution.session import active_batch

RECEIPTS = ".cw/review-infrastructure-recoveries"
AUTHORIZATIONS = ".cw/review-retry-authorizations"

def pending_legacy_authorization(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    directory = root / AUTHORIZATIONS
    if not directory.is_dir():
        return None
    matches = []
    for path in directory.glob("*.json"):
        try:
            payload = load_json(path)
        except CwError:
            continue
        if isinstance(payload, dict) and payload.get("status") == "PENDING":
            # A retry that fails technically is allowed to leave controlled
            # retry metadata in state.  That failure must not silently spend
            # the human authorization or require a new one; only the exact
            # recorded authorization id can resume it after that boundary.
            current_state = sha256_file(root / ".cw/state.json")
            if (
                payload.get("state_sha256_before") == current_state
                or state.get("legacy_retry_authorization_id")
                == payload.get("authorization_id")
            ):
                matches.append(payload)
    if len(matches) > 1:
        raise CwError("Multiple legacy retry authorizations are active", ErrorCode.OPERATION_CONFLICT)
    return matches[0] if matches else None

def consume_legacy_authorization(root: Path, authorization_id: str, review_ref: str) -> None:
    path = root / AUTHORIZATIONS / f"{authorization_id}.json"
    payload = load_json(path)
    if not isinstance(payload, dict) or payload.get("status") != "PENDING":
        raise CwError("Legacy retry authorization is already consumed", ErrorCode.OPERATION_CONFLICT)
    payload = dict(payload)
    payload.update({"status": "CONSUMED", "consumed_at": utc_now(), "consuming_review_ref": review_ref})
    atomic_json(path, payload)
SUPERSESSIONS = ".cw/review-infrastructure-supersessions"
TRANSACTION = ".cw/runtime/review-infrastructure-recovery.json"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REFERENCE = re.compile(r"\.cw/reviews/[A-Za-z0-9._-]+\.json")
_INFRA = re.compile(
    r"(?i)(permission denied|read-only|operation not permitted|not writable|cache|tmp|temp(?:orary)?)"
)
_COMMAND = re.compile(r'"type"\s*:\s*"command_execution"|command_execution')
_PROJECT_TOOLS = ("composer", "phpunit", "phpstan", "pint", "pytest", "ruff", "mypy")
FailureInjector = Callable[[str], None]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _safe_regular(root: Path, reference: str, parent: str) -> Path:
    if (
        Path(reference).is_absolute()
        or ".." in Path(reference).parts
        or not reference.startswith(parent + "/")
    ):
        raise CwError("Review recovery reference is unsafe", ErrorCode.INTEGRITY_ERROR)
    path = root / reference
    cursor = root
    for component in Path(reference).parts[:-1]:
        cursor /= component
        try:
            if cursor.is_symlink() or not cursor.is_dir():
                raise CwError(
                    "Review recovery namespace is unsafe", ErrorCode.INTEGRITY_ERROR
                )
        except OSError as exc:
            raise CwError(
                "Review recovery namespace is unsafe", ErrorCode.INTEGRITY_ERROR
            ) from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CwError(
            "Review recovery evidence is missing", ErrorCode.INTEGRITY_ERROR
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CwError(
            "Review recovery evidence file is unsafe", ErrorCode.INTEGRITY_ERROR
        )
    return path


def _safe_directory(root: Path, reference: str, *, create: bool = False) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise CwError("Review recovery namespace is unsafe", ErrorCode.INTEGRITY_ERROR)
    path = root / reference
    cursor = root
    for component in relative.parts:
        cursor /= component
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or not cursor.is_dir():
                raise CwError(
                    "Review recovery namespace is unsafe", ErrorCode.INTEGRITY_ERROR
                )
            continue
        if not create:
            raise CwError(
                "Review recovery namespace is missing", ErrorCode.INTEGRITY_ERROR
            )
        try:
            cursor.mkdir(mode=0o700)
        except OSError as exc:
            raise CwError(
                "Review recovery namespace is unsafe", ErrorCode.INTEGRITY_ERROR
            ) from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CwError("Review recovery namespace is missing", ErrorCode.INTEGRITY_ERROR) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CwError("Review recovery namespace is unsafe", ErrorCode.INTEGRITY_ERROR)
    expected_uid = getattr(os, "getuid", lambda: metadata.st_uid)()
    if metadata.st_uid != expected_uid:
        raise CwError("Review recovery namespace owner is unsafe", ErrorCode.INTEGRITY_ERROR)
    return path


def _request(
    workflow: str,
    phase: str,
    review_reference: str,
    review_sha256: str,
    workflow_sha256: str,
    state_sha256: str,
    reason: str,
) -> dict[str, str]:
    return {
        "workflow": workflow,
        "phase": phase,
        "review_reference": review_reference,
        "review_sha256": review_sha256,
        "workflow_sha256": workflow_sha256,
        "state_sha256": state_sha256,
        "reason": reason.strip(),
    }


def _identifier(request: dict[str, str]) -> str:
    return "rir-" + sha256_bytes(_canonical(request)).removeprefix("sha256:")


def _proof(
    root: Path, workflow: Any, phase: Any, review: dict[str, Any]
) -> dict[str, str]:
    evidence = review.get("validation_evidence")
    receipt = (
        evidence.get("verification_receipt") if isinstance(evidence, dict) else None
    )
    if not isinstance(receipt, dict) or not {"reference", "sha256"}.issubset(receipt):
        raise CwError(
            "Historical reviewer infrastructure is not demonstrable",
            ErrorCode.INVALID_STATE,
        )
    validated = validate_verification_receipt(
        root, workflow, phase, receipt["reference"], receipt["sha256"]
    )
    # Narrative logs are not provenance. Only a structured event persisted in
    # the original receipt can prove infrastructure; this candidate has none.
    events = review.get("infrastructure_events")
    if not isinstance(events, list) or not events:
        raise CwError("Historical review does not contain linked infrastructure evidence", ErrorCode.INVALID_STATE)
    return {
        "verification_receipt": receipt["reference"],
        "verification_receipt_sha256": receipt["sha256"],
        "verification_receipt_digest": validated["receipt_sha256"],
        "proof": "validated_receipt+linked_structured_infrastructure_event",
    }


def _preflight(
    root: Path,
    phase_id: str,
    review_reference: str,
    expected_review_sha256: str,
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    allow_journal: bool = False,
    require_proof: bool = True,
) -> dict[str, Any]:
    if not all(
        _DIGEST.fullmatch(value or "")
        for value in (
            expected_review_sha256,
            expected_workflow_sha256,
            expected_state_sha256,
        )
    ):
        raise CwError(
            "Review recovery requires canonical SHA-256 values",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    if not reason.strip():
        raise CwError(
            "Review recovery requires a reason", ErrorCode.USAGE_ERROR, exit_code=2
        )
    if _REFERENCE.fullmatch(review_reference or "") is None:
        raise CwError(
            "Review reference is not canonical", ErrorCode.USAGE_ERROR, exit_code=2
        )
    _safe_directory(root, ".cw/runtime")
    _safe_regular(root, ".cw/state.json", ".cw")
    journal_candidate = root / TRANSACTION
    if (journal_candidate.exists() or journal_candidate.is_symlink()) and not allow_journal:
        raise CwError(
            "Review recovery transaction requires recovery",
            ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
        )
    lock = root / ".cw/locks/operation.lock"
    if lock.exists() or lock.is_symlink():
        if lock.is_symlink() or not lock.is_file():
            raise CwError("CW operation lock is unsafe", ErrorCode.LOCKED)
        try:
            lock_data = load_json(lock)
            lock_pid = (
                int(lock_data.get("pid", 0)) if isinstance(lock_data, dict) else 0
            )
        except (CwError, TypeError, ValueError):
            lock_pid = 0
        from cw.core.platform import process_is_alive

        if lock_pid and lock_pid != os.getpid() and process_is_alive(lock_pid):
            raise CwError("Another CW operation is active", ErrorCode.LOCKED)
    if load_active_run(root) is not None:
        raise CwError("A CW run is active", ErrorCode.LOCKED)
    if session_path(root).exists() or session_path(root).is_symlink():
        raise CwError("An implementation session is active or stale", ErrorCode.LOCKED)
    if active_batch(root, own_pid=os.getpid()) is not None:
        raise CwError("A CW batch is active", ErrorCode.LOCKED)
    workflow = load_workflow(root)
    state = load_state(root)
    if workflow_hash(root / ".codex/workflow/phases.yaml") != expected_workflow_sha256:
        raise CwError(
            "Workflow changed before review recovery", ErrorCode.STALE_WORKFLOW_SHA
        )
    if sha256_file(root / ".cw/state.json") != expected_state_sha256:
        raise CwError("State changed before review recovery", ErrorCode.STALE_STATE_SHA)
    if (
        state.get("status") != WorkflowState.REVISION_REQUIRED.value
        or state.get("current_phase") != phase_id
    ):
        raise CwError(
            "Review recovery requires the active REVISION_REQUIRED phase",
            ErrorCode.INVALID_STATE,
        )
    if state.get("last_review") != review_reference:
        raise CwError("Selected review is not active", ErrorCode.OPERATION_CONFLICT)
    phase = workflow.phase(phase_id)
    if gate_path(root, phase_id).exists():
        raise CwError(
            "Approved or gated phases cannot be recovered", ErrorCode.INVALID_STATE
        )
    review_path = _safe_regular(root, review_reference, ".cw/reviews")
    if sha256_file(review_path) != expected_review_sha256:
        raise CwError("Review digest changed", ErrorCode.INTEGRITY_ERROR)
    review = load_json(review_path)
    if (
        not isinstance(review, dict)
        or review.get("kind") != "semantic_review"
        or review.get("decision") != "REVISE"
        or review.get("workflow") != workflow.id
        or review.get("phase") != phase_id
    ):
        raise CwError(
            "Selected review is not a recoverable semantic REVISE",
            ErrorCode.INVALID_STATE,
        )
    from cw.core.reviews import validate_reviewer_result

    semantic_payload = {
        key: review.get(key)
        for key in ("decision", "summary", "blocking_issues", "blocking_criteria")
    }
    semantic_payload["criteria"] = [
        {key: item.get(key) for key in ("id", "status", "evidence")}
        for item in review.get("criteria", [])
        if isinstance(item, dict)
    ]
    decision, _, _, _ = validate_reviewer_result(
        phase,
        semantic_payload,
        require_blocking_criteria=True,
        strict=True,
        root=root,
    )
    if decision.value != "REVISE":
        raise CwError(
            "Selected review schema does not prove REVISE", ErrorCode.INTEGRITY_ERROR
        )
    attempt = review.get("attempt")
    revision_attempt = review.get("revision_attempt", attempt)
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or state.get("attempt") != attempt
        or state.get("revision_attempt", state.get("attempt")) != revision_attempt
    ):
        raise CwError(
            "Review attempts do not match active state", ErrorCode.OPERATION_CONFLICT
        )
    proof = _proof(root, workflow, phase, review) if require_proof else {"proof": "legacy-human-authorization"}
    supersession_directory = root / SUPERSESSIONS
    if supersession_directory.exists():
        _safe_directory(root, SUPERSESSIONS)
        for path in supersession_directory.glob("*.json"):
            payload = load_json(
                _safe_regular(root, path.relative_to(root).as_posix(), SUPERSESSIONS)
            )
            if isinstance(payload, dict) and payload.get("review_reference") == review_reference:
                raise CwError("Selected review is already superseded", ErrorCode.OPERATION_CONFLICT)
    request = _request(
        workflow.id,
        phase_id,
        review_reference,
        expected_review_sha256,
        expected_workflow_sha256,
        expected_state_sha256,
        reason,
    )
    return {
        "workflow": workflow,
        "state": state,
        "review": review,
        "proof": proof,
        "request": request,
        "recovery_id": _identifier(request),
    }


def _result(
    prepared: dict[str, Any],
    *,
    changed: bool,
    replay: bool,
    backup: str | None = None,
    receipt: str | None = None,
) -> dict[str, Any]:
    request = prepared["request"]
    return {
        "result": "RECOVERED" if changed or replay else "RECOVERY_PREVIEW",
        "changed": changed,
        "mutation": "state+append-only-evidence" if changed else "none",
        "idempotent_replay": replay,
        "retryable": changed or replay,
        "classification": ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR.value,
        "operation_id": prepared["recovery_id"],
        "recovery_id": prepared["recovery_id"],
        "phase": request["phase"],
        "review_reference": request["review_reference"],
        "review_sha256": request["review_sha256"],
        "workflow_sha256": request["workflow_sha256"],
        "state_sha256": request["state_sha256"],
        "attempts_restored": 1,
        "readiness_available": False,
        "backup": backup,
        "recovery_receipt": receipt,
        "next_action": "cw retry --json",
    }


def preview_review_infrastructure_recovery(root: Path, *args: str) -> dict[str, Any]:
    return _result(_preflight(root, *args), changed=False, replay=False)

def authorize_legacy_retry(root: Path, phase_id: str, review_reference: str, review_sha256: str,
                           workflow_sha256: str, state_sha256: str, reason: str,
                           acknowledgement: bool, *, apply: bool) -> dict[str, Any]:
    if not acknowledgement or not reason.strip():
        raise CwError("Legacy retry requires explicit acknowledgement and reason", ErrorCode.USAGE_ERROR, exit_code=2)
    prepared = _preflight(root, phase_id, review_reference, review_sha256, workflow_sha256, state_sha256, reason, require_proof=False)
    request = prepared["request"]
    aid = "lra-" + sha256_bytes(_canonical(request)).removeprefix("sha256:")
    directory = root / AUTHORIZATIONS
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{aid}.json"
    payload = {"schema": "cw.legacy-review-retry-authorization.v1", "authorization_id": aid,
               "operation_id": aid, "correlation_id": aid[4:20],
               "classification": "HUMAN_AUTHORIZED_LEGACY_REVIEW_RETRY", "phase_id": phase_id,
               "review_ref": review_reference, "review_sha256": review_sha256,
               "workflow_sha256": workflow_sha256, "state_sha256_before": state_sha256,
               "plan_revision": prepared["review"].get("plan_revision_id"), "reason": reason.strip(),
               "acknowledgement": True, "created_at": utc_now(), "request_digest": sha256_bytes(_canonical(request)),
               "status": "PENDING", "consumed_at": None, "consuming_review_ref": None}
    payload["request_digest"] = sha256_bytes(_canonical({k:v for k,v in payload.items() if k not in {"request_digest", "created_at"}}))
    if path.exists():
        existing = load_json(path)
        if existing == payload or (isinstance(existing, dict) and existing.get("request_digest") == payload["request_digest"]):
            return {"result": "AUTHORIZATION_PREVIEW" if not apply else "AUTHORIZED", "changed": False,
                    "idempotent_replay": bool(apply), "classification": payload["classification"], "authorization_id": aid,
                    "authorization_status": existing.get("status", "PENDING"), "verification_required": True,
                    "next_action": "cw retry --json"}
        raise CwError("Legacy retry authorization conflicts with an existing request", ErrorCode.OPERATION_CONFLICT)
    for existing_path in directory.glob("*.json"):
        existing = load_json(existing_path)
        if isinstance(existing, dict) and existing.get("status") == "PENDING":
            raise CwError(
                "A legacy retry authorization is already pending",
                ErrorCode.OPERATION_CONFLICT,
            )
    if not apply:
        return {"result": "AUTHORIZATION_PREVIEW", "changed": False, "idempotent_replay": False,
                "classification": payload["classification"], "authorization_id": aid, "authorization_status": "PENDING",
                "verification_required": True, "next_action": "cw review authorize-retry --apply ..."}
    atomic_json_new(path, payload)
    return {"result": "AUTHORIZED", "changed": True, "idempotent_replay": False,
            "classification": payload["classification"], "authorization_id": aid, "authorization_status": "PENDING",
            "verification_required": True, "next_action": "cw retry --json"}


def recover_review_infrastructure_transaction(root: Path) -> dict[str, Any] | None:
    journal_path = root / TRANSACTION
    if not journal_path.exists():
        return None
    journal = load_json(_safe_regular(root, TRANSACTION, ".cw/runtime"))
    if (
        not isinstance(journal, dict)
        or journal.get("kind") != "review_infrastructure_recovery_transaction"
        or journal.get("status") not in {"PREPARED", "BACKUP_READY", "COMMITTED"}
    ):
        raise CwError(
            "Review recovery journal is corrupt",
            ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
        )
    backup = root / str(journal.get("backup"))
    if journal["status"] == "COMMITTED":
        receipt = _safe_regular(root, str(journal["receipt"]), RECEIPTS)
        if sha256_file(receipt) != journal.get("receipt_sha256"):
            raise CwError(
            "Committed review recovery receipt is invalid",
                ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
            )
        payload = load_json(receipt)
        if not isinstance(payload, dict):
            raise CwError(
                "Committed review recovery receipt is invalid",
                ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
            )
        validate_review_infrastructure_recovery_receipt(root, receipt, payload)
        journal_path.unlink()
        return {"committed": True}
    old_state = journal.get("old_state")
    if not isinstance(old_state, dict):
        raise CwError(
            "Review recovery journal has no rollback state",
            ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
        )
    atomic_json(root / ".cw/state.json", old_state)
    for reference in (journal.get("receipt"), journal.get("supersession")):
        if isinstance(reference, str):
            path = root / reference
            if path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1:
                path.unlink()
    if backup.exists():
        if backup.is_symlink() or not backup.is_dir():
            raise CwError(
                "Review recovery backup is unsafe",
                ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
            )
        shutil.rmtree(backup)
    journal_path.unlink()
    return {"recovered": True}


def validate_review_infrastructure_recovery_receipt(
    root: Path,
    path: Path,
    payload: dict[str, Any],
    *,
    require_current_state: bool = True,
) -> None:
    if payload.get("kind") != "review_infrastructure_recovery_receipt":
        raise CwError("Review recovery receipt kind is invalid", ErrorCode.INTEGRITY_ERROR)
    expected = sha256_bytes(
        _canonical({key: value for key, value in payload.items() if key != "receipt_sha256"})
    )
    if payload.get("receipt_sha256") != expected or path.name != f"{payload.get('recovery_id')}.json":
        raise CwError("Review recovery receipt integrity failed", ErrorCode.INTEGRITY_ERROR)
    request = payload.get("request")
    if not isinstance(request, dict) or _identifier(request) != payload.get("recovery_id"):
        raise CwError("Review recovery request identity failed", ErrorCode.INTEGRITY_ERROR)
    review = _safe_regular(root, str(request.get("review_reference")), ".cw/reviews")
    if sha256_file(review) != request.get("review_sha256"):
        raise CwError("Recovered original review changed", ErrorCode.INTEGRITY_ERROR)
    backup_reference = str(payload.get("backup"))
    backup = _safe_directory(root, backup_reference)
    validate_tree(backup, "Review infrastructure recovery backup")
    backup_digest = sha256_bytes(
        _canonical({
            "state": sha256_file(_safe_regular(root, f"{backup_reference}/state.json", backup_reference)),
            "review": sha256_file(_safe_regular(root, f"{backup_reference}/review.json", backup_reference)),
        })
    )
    if backup_digest != payload.get("backup_sha256"):
        raise CwError("Review recovery backup integrity failed", ErrorCode.INTEGRITY_ERROR)
    supersession = _safe_regular(root, str(payload.get("supersession")), SUPERSESSIONS)
    supersession_payload = load_json(supersession)
    if (
        not isinstance(supersession_payload, dict)
        or supersession_payload.get("recovery_id") != payload.get("recovery_id")
        or supersession_payload.get("supersession_sha256")
        != sha256_bytes(
            _canonical(
                {
                    key: value
                    for key, value in supersession_payload.items()
                    if key != "supersession_sha256"
                }
            )
        )
    ):
        raise CwError("Review supersession integrity failed", ErrorCode.INTEGRITY_ERROR)
    if require_current_state and sha256_file(root / ".cw/state.json") != payload.get("after_state_sha256"):
        raise CwError("Recovered state changed after apply", ErrorCode.OPERATION_CONFLICT)


def apply_review_infrastructure_recovery(
    root: Path, *args: str, failure_injector: FailureInjector | None = None
) -> dict[str, Any]:
    recover_review_infrastructure_transaction(root)
    workflow_id = load_workflow(root).id
    request = _request(workflow_id, *args)
    recovery_id = _identifier(request)
    receipt_ref = f"{RECEIPTS}/{recovery_id}.json"
    receipt_path = root / receipt_ref
    if receipt_path.exists():
        payload = load_json(_safe_regular(root, receipt_ref, RECEIPTS))
        if payload.get("request") != request:
            raise CwError(
                "Review recovery replay conflicts with persisted evidence",
                ErrorCode.OPERATION_CONFLICT,
            )
        validate_review_infrastructure_recovery_receipt(root, receipt_path, payload)
        return _result(
            {"request": request, "recovery_id": recovery_id},
            changed=False,
            replay=True,
            backup=payload["backup"],
            receipt=receipt_ref,
        )
    for existing in (
        (root / RECEIPTS).glob("*.json") if (root / RECEIPTS).is_dir() else ()
    ):
        payload = load_json(existing)
        if (
            isinstance(payload, dict)
            and payload.get("request", {}).get("state_sha256")
            == request["state_sha256"]
        ):
            raise CwError(
                "Review recovery state CAS was already consumed",
                ErrorCode.OPERATION_CONFLICT,
            )
    prepared = _preflight(root, *args)
    old_state = copy.deepcopy(prepared["state"])
    backup_ref = f".cw/backups/{recovery_id}"
    backup = root / backup_ref
    supersession_ref = f"{SUPERSESSIONS}/{recovery_id}.json"
    journal = {
        "schema_version": SCHEMA_VERSION,
        "kind": "review_infrastructure_recovery_transaction",
        "status": "PREPARED",
        "recovery_id": recovery_id,
        "old_state": old_state,
        "backup": backup_ref,
        "backup_sha256": None,
        "receipt": receipt_ref,
        "receipt_sha256": None,
        "supersession": supersession_ref,
    }
    _safe_directory(root, ".cw/backups", create=True)
    atomic_json_new(root / TRANSACTION, journal)
    step = failure_injector or (lambda _name: None)
    try:
        step("prepared")
        backup.mkdir(mode=0o700)
        shutil.copy2(
            root / ".cw/state.json", backup / "state.json", follow_symlinks=False
        )
        shutil.copy2(
            root / prepared["request"]["review_reference"],
            backup / "review.json",
            follow_symlinks=False,
        )
        for copied in (backup / "state.json", backup / "review.json"):
            with copied.open("rb") as stream:
                os.fsync(stream.fileno())
        fsync_directory(backup)
        step("backup_fsync")
        validate_tree(backup, "Review infrastructure recovery backup")
        backup_digest = sha256_bytes(
            _canonical(
                {
                    name: sha256_file(path)
                    for name, path in (
                        ("state", backup / "state.json"),
                        ("review", backup / "review.json"),
                    )
                }
            )
        )
        journal.update({"status": "BACKUP_READY", "backup_sha256": backup_digest})
        atomic_json(root / TRANSACTION, journal)
        step("backup_ready")
        again = _preflight(root, *args, allow_journal=True)
        if again["recovery_id"] != recovery_id or again["proof"] != prepared["proof"]:
            raise CwError(
                "Review recovery evidence changed after backup",
                ErrorCode.OPERATION_CONFLICT,
            )
        after = copy.deepcopy(old_state)
        after["attempt"] = int(after["attempt"]) - 1
        after["revision_attempt"] = (
            int(after.get("revision_attempt", old_state["attempt"])) - 1
        )
        after["status"] = WorkflowState.ERROR.value
        after["last_error"] = (
            f"{ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR.value}: Historical semantic review recovered"
        )
        after["infrastructure_error"] = {
            "error_code": ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR.value,
            "retryable": True,
            "operation": "review",
            "phase": prepared["request"]["phase"],
            "occurred_at": utc_now(),
            "legacy": True,
        }
        after.setdefault("history", []).append(
            {
                "timestamp": utc_now(),
                "phase": prepared["request"]["phase"],
                "action": "semantic_review_superseded_as_infrastructure",
                "review": prepared["request"]["review_reference"],
                "recovery_id": recovery_id,
                "attempt_restored": 1,
                "recovery_receipt": receipt_ref,
                "backup": backup_ref,
            }
        )
        from cw import __version__

        after["cw_version"] = __version__
        after["updated_at"] = utc_now()
        supersession = {
            "schema_version": SCHEMA_VERSION,
            "kind": "review_infrastructure_supersession",
            "recovery_id": recovery_id,
            "review_reference": prepared["request"]["review_reference"],
            "review_sha256": prepared["request"]["review_sha256"],
            "reason": prepared["request"]["reason"],
            "proof": prepared["proof"],
            "created_at": utc_now(),
        }
        supersession["supersession_sha256"] = sha256_bytes(_canonical(supersession))
        _safe_directory(root, SUPERSESSIONS, create=True)
        _safe_directory(root, RECEIPTS, create=True)
        atomic_json_new(root / supersession_ref, supersession)
        step("supersession")
        atomic_json(root / ".cw/state.json", after)
        step("state")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "kind": "review_infrastructure_recovery_receipt",
            "recovery_id": recovery_id,
            "request": prepared["request"],
            "proof": prepared["proof"],
            "backup": backup_ref,
            "backup_sha256": backup_digest,
            "supersession": supersession_ref,
            "before_state_sha256": prepared["request"]["state_sha256"],
            "after_state_sha256": sha256_file(root / ".cw/state.json"),
            "created_at": utc_now(),
        }
        receipt["receipt_sha256"] = sha256_bytes(_canonical(receipt))
        atomic_json_new(receipt_path, receipt)
        step("receipt")
        journal.update(
            {"status": "COMMITTED", "receipt_sha256": sha256_file(receipt_path)}
        )
        atomic_json(root / TRANSACTION, journal)
        step("committed")
        step("cleanup")
        (root / TRANSACTION).unlink()
        fsync_directory((root / TRANSACTION).parent)
        return _result(
            prepared, changed=True, replay=False, backup=backup_ref, receipt=receipt_ref
        )
    except Exception:
        recover_review_infrastructure_transaction(root)
        raise
