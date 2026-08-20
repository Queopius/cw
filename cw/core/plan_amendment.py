from __future__ import annotations

import copy
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audit import audit_history
from .completion import contract_hash, contract_payload
from .errors import CwError, ErrorCode
from .initialize import backup_metadata
from .models import CompletionContract, Workflow, WorkflowState
from .platform import fsync_directory
from .state import load_state, save_state, validate_state
from .utils import (
    atomic_json,
    atomic_write_bytes,
    load_json,
    safe_project_path,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .workflow import (
    load_workflow,
    workflow_document_from_text,
    workflow_from_document,
    workflow_hash,
    write_workflow,
)

TRANSACTION = ".cw/runtime/plan-amend-transaction.json"
_SHA256 = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})")
FailureInjector = Callable[[str], None]


def ensure_no_pending_plan_amendment(root: Path) -> None:
    if (root / TRANSACTION).exists():
        raise CwError(
            "An interrupted plan amendment requires recovery",
            ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
            "Repeat the same cw plan amend command to restore the recorded backup before retrying.",
            exit_code=3,
        )


def _normalized_sha256(value: str) -> str:
    match = _SHA256.fullmatch(value)
    if match is None:
        raise CwError(
            "Expected workflow SHA-256 is invalid",
            ErrorCode.USAGE_ERROR,
            "Supply the exact value reported by cw plan show --json.",
            exit_code=2,
        )
    return "sha256:" + match.group(1).lower()


def _canonical_contract(workflow: Workflow) -> tuple[dict[str, Any] | None, str | None]:
    contract = workflow.completion_target
    return (
        contract_payload(contract) if contract is not None else None,
        contract_hash(contract) if contract is not None else None,
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _ensure_safe_input(
    root: Path, value: str
) -> tuple[Path, tuple[int, int, int, int, int]]:
    try:
        path = safe_project_path(root, value, must_exist=True)
    except CwError as exc:
        raise CwError(
            "Plan amendment file is invalid or missing",
            ErrorCode.USAGE_ERROR,
            details=exc.message,
            exit_code=2,
        ) from exc
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CwError(
            "Plan amendment file is invalid or missing",
            ErrorCode.USAGE_ERROR,
            details=str(exc),
            exit_code=2,
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise CwError(
            "Plan amendment file is unsafe", ErrorCode.USAGE_ERROR, exit_code=2
        )
    relative = path.relative_to(root).as_posix()
    if relative == ".git" or relative.startswith((".git/", ".cw/", ".codex/")):
        raise CwError(
            "Plan amendment file must be outside governed metadata",
            ErrorCode.USAGE_ERROR,
            exit_code=2,
        )
    return path, _file_identity(metadata)


def _active_artifacts(root: Path) -> list[str]:
    paths = (
        ".cw/runtime/implementer-session.json",
        ".cw/runtime/READY_FOR_REVIEW.json",
        ".cw/runtime/active-run.json",
        ".cw/runtime/batch.json",
    )
    active = [value for value in paths if (root / value).exists()]
    for directory in (".cw/gates", ".cw/reviews"):
        parent = root / directory
        if parent.is_dir() and any(path.is_file() for path in parent.iterdir()):
            active.append(directory)
    completion = root / ".cw/completion"
    if (completion / "completion.satisfied.json").exists():
        active.append(".cw/completion/completion.satisfied.json")
    for directory in ("reviews", "proposals", "authorizations"):
        parent = completion / directory
        if parent.is_dir() and any(path.is_file() for path in parent.iterdir()):
            active.append(f".cw/completion/{directory}")
    return active


def _serialize_workflow(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_input_bytes(
    path: Path, expected_identity: tuple[int, int, int, int, int]
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CwError(
            "Plan amendment file cannot be read safely",
            ErrorCode.USAGE_ERROR,
            details=str(exc),
            exit_code=2,
        ) from exc
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or _file_identity(opened_metadata) != expected_identity
        ):
            raise CwError(
                "Plan amendment file changed before it could be read safely",
                ErrorCode.USAGE_ERROR,
                exit_code=2,
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read()
            final_metadata = os.fstat(stream.fileno())
            if _file_identity(final_metadata) != expected_identity:
                raise CwError(
                    "Plan amendment file changed while it was being read",
                    ErrorCode.USAGE_ERROR,
                    exit_code=2,
                )
            return content
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore(root: Path, transaction: dict[str, Any]) -> None:
    backup_value = transaction.get("backup")
    if not isinstance(backup_value, str) or not backup_value.startswith(".cw/backups/"):
        raise CwError(
            "Plan amendment backup reference is invalid",
            ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
        )
    backup = safe_project_path(root, backup_value, must_exist=True)
    old_workflow = backup / "phases.yaml"
    old_state = backup / "state.json"
    if not old_workflow.is_file() or not old_state.is_file():
        raise CwError(
            "Plan amendment backup is incomplete", ErrorCode.PLAN_AMEND_ROLLBACK_FAILED
        )
    if sha256_file(old_workflow) != transaction.get(
        "previous_workflow_sha256"
    ) or sha256_file(old_state) != transaction.get("previous_state_sha256"):
        raise CwError(
            "Plan amendment backup integrity failed",
            ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
        )
    atomic_write_bytes(root / ".codex/workflow/phases.yaml", old_workflow.read_bytes())
    atomic_write_bytes(root / ".cw/state.json", old_state.read_bytes())
    if (
        workflow_hash(root / ".codex/workflow/phases.yaml")
        != transaction["previous_workflow_sha256"]
        or sha256_file(root / ".cw/state.json") != transaction["previous_state_sha256"]
    ):
        raise CwError(
            "Plan amendment rollback verification failed",
            ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
        )


def _remove_transaction(path: Path) -> None:
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def recover_plan_amendment(root: Path) -> bool:
    path = root / TRANSACTION
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise CwError(
            "Plan amendment transaction is unsafe", ErrorCode.PLAN_AMEND_ROLLBACK_FAILED
        )
    transaction = load_json(path)
    if not isinstance(transaction, dict) or transaction.get("kind") != "plan_amend":
        raise CwError(
            "Plan amendment transaction is invalid",
            ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
        )
    _restore(root, transaction)
    _remove_transaction(path)
    return True


def _preflight(
    root: Path,
    input_path: Path,
    input_identity: tuple[int, int, int, int, int],
    expected_workflow_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], Workflow, str, bytes, str | None, str]:
    state = load_state(root)
    workflow = load_workflow(root)
    if (
        WorkflowState(str(state.get("status"))) is not WorkflowState.PLAN_PROPOSED
        or workflow.status != "PROPOSED"
    ):
        raise CwError(
            "Plan amendment is allowed only for an unapproved proposed plan",
            ErrorCode.INVALID_STATE,
            exit_code=3,
        )
    validate_state(root, state, workflow)
    audit_history(root, workflow, state)
    active = _active_artifacts(root)
    if active:
        raise CwError(
            "Plan amendment is blocked by active or incompatible workflow evidence",
            ErrorCode.INVALID_STATE,
            details="\n".join(active),
            exit_code=3,
        )
    current_sha = workflow_hash(root / ".codex/workflow/phases.yaml")
    if _normalized_sha256(expected_workflow_sha256) != current_sha:
        raise CwError(
            "Workflow changed since the amendment was prepared",
            ErrorCode.STALE_WORKFLOW_SHA,
            "Reload the proposal and prepare a new amendment against its current SHA-256.",
            exit_code=4,
        )
    input_bytes = _read_input_bytes(input_path, input_identity)
    try:
        proposed_document = workflow_document_from_text(input_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CwError(
            "Plan amendment file is not UTF-8",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
            details=str(exc),
            exit_code=2,
        ) from exc
    except CwError as exc:
        if exc.code is ErrorCode.SCHEMA_VALIDATION_ERROR:
            raise CwError(
                exc.message, exc.code, exc.hint, exc.details, exit_code=2
            ) from exc
        raise
    if not isinstance(proposed_document.get("workflow"), dict) or not isinstance(
        proposed_document.get("phases"), list
    ):
        raise CwError(
            "Invalid workflow structure", ErrorCode.SCHEMA_VALIDATION_ERROR, exit_code=2
        )
    current_contract, current_contract_sha = _canonical_contract(workflow)
    raw_contract = proposed_document.get("completion_target")
    try:
        proposed_contract = (
            contract_payload(CompletionContract.from_dict(raw_contract))
            if isinstance(raw_contract, dict)
            else None
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CwError(
            "Completion Contract changes require a plan rebuild",
            ErrorCode.COMPLETION_CONTRACT_CHANGE_REQUIRES_REBUILD,
            details=str(exc),
            exit_code=3,
        ) from exc
    if proposed_contract != current_contract:
        raise CwError(
            "Completion Contract changes require a plan rebuild",
            ErrorCode.COMPLETION_CONTRACT_CHANGE_REQUIRES_REBUILD,
            'Run: cw plan rebuild --goal "..."',
            exit_code=3,
        )
    try:
        proposed_workflow = workflow_from_document(root, proposed_document)
    except CwError as exc:
        if exc.code is ErrorCode.SCHEMA_VALIDATION_ERROR:
            raise CwError(
                exc.message, exc.code, exc.hint, exc.details, exit_code=2
            ) from exc
        raise
    if proposed_workflow.status != "PROPOSED":
        raise CwError(
            "Amended workflow must remain PROPOSED",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
            exit_code=2,
        )
    if (
        proposed_workflow.id != workflow.id
        or proposed_workflow.repository != workflow.repository
    ):
        raise CwError(
            "Amended plan belongs to another project",
            ErrorCode.WORKFLOW_PROJECT_MISMATCH,
            exit_code=2,
        )
    serialized = _serialize_workflow(proposed_document)
    new_sha = sha256_bytes(serialized)
    if new_sha == current_sha:
        raise CwError(
            "Amended workflow is unchanged", ErrorCode.USAGE_ERROR, exit_code=2
        )
    return (
        state,
        proposed_document,
        proposed_workflow,
        current_sha,
        serialized,
        current_contract_sha,
        sha256_bytes(input_bytes),
    )


def amend_plan(
    root: Path,
    file: str,
    expected_workflow_sha256: str,
    *,
    failure_injector: FailureInjector | None = None,
) -> dict[str, Any]:
    transaction_path = root / TRANSACTION
    recover_plan_amendment(root)
    input_path, input_identity = _ensure_safe_input(root, file)
    (
        state,
        document,
        proposed,
        previous_sha,
        serialized,
        contract_sha,
        input_sha,
    ) = _preflight(
        root,
        input_path,
        input_identity,
        expected_workflow_sha256,
    )
    previous_state_sha = sha256_file(root / ".cw/state.json")
    new_sha = sha256_bytes(serialized)
    backup = backup_metadata(root)
    backup_relative = backup.relative_to(root).as_posix()
    transaction = {
        "kind": "plan_amend",
        "created_at": utc_now(),
        "backup": backup_relative,
        "previous_workflow_sha256": previous_sha,
        "workflow_sha256": new_sha,
        "previous_state_sha256": previous_state_sha,
        "input_sha256": input_sha,
    }
    atomic_json(transaction_path, transaction)
    try:
        if failure_injector:
            failure_injector("before_workflow_write")
        write_workflow(root / ".codex/workflow/phases.yaml", document)
        if failure_injector:
            failure_injector("after_workflow_write")
        amended_state = copy.deepcopy(state)
        amended_state.update(
            {
                "workflow_id": proposed.id,
                "workflow_version": proposed.version,
                "workflow_sha256": new_sha,
                "current_phase": proposed.phases[0].id if proposed.phases else None,
                "status": WorkflowState.PLAN_PROPOSED.value,
                "attempt": 0,
                "last_review": None,
                "last_gate": None,
                "last_error": None,
                "infrastructure_error": None,
                "pending_goal": None,
            }
        )
        amended_state.setdefault("history", []).append(
            {
                "timestamp": utc_now(),
                "phase": None,
                "action": "plan_amended",
                "previous_workflow_sha256": previous_sha,
                "workflow_sha256": new_sha,
                "completion_contract_sha256": contract_sha or "none",
                "input_sha256": input_sha,
                "backup": backup_relative,
            }
        )
        save_state(root, amended_state)
        if failure_injector:
            failure_injector("after_state_write")
        reloaded_workflow = load_workflow(root)
        reloaded_state = load_state(root)
        validate_state(root, reloaded_state, reloaded_workflow)
        audit_history(root, reloaded_workflow, reloaded_state)
        if (
            reloaded_state.get("status") != WorkflowState.PLAN_PROPOSED.value
            or reloaded_workflow.status != "PROPOSED"
            or reloaded_state.get("workflow_sha256") != new_sha
            or _canonical_contract(reloaded_workflow)[1] != contract_sha
            or _active_artifacts(root)
        ):
            raise CwError(
                "Amended plan failed integrity verification",
                ErrorCode.PLAN_AMEND_INTEGRITY_ERROR,
            )
    except BaseException as exc:
        try:
            _restore(root, transaction)
            _remove_transaction(transaction_path)
        except Exception as rollback_exc:
            raise CwError(
                "Plan amendment rollback failed",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
                "Restore the recorded backup before any workflow operation.",
                details=str(rollback_exc),
            ) from rollback_exc
        if isinstance(exc, CwError):
            raise
        raise CwError(
            "Plan amendment failed and was rolled back",
            ErrorCode.PLAN_AMEND_INTEGRITY_ERROR,
            details=f"{type(exc).__name__}: {exc}",
        ) from exc
    _remove_transaction(transaction_path)
    return {
        "amended": True,
        "status": WorkflowState.PLAN_PROPOSED.value,
        "backup": backup_relative,
        "previous_workflow_sha256": previous_sha,
        "workflow_sha256": new_sha,
        "phase_count": len(proposed.phases),
        "completion_contract_preserved": True,
        "approval_required": True,
    }
