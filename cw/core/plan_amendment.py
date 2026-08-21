from __future__ import annotations

import copy
import fnmatch
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
from .layout import safe_directory
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
    _read_document,
    load_workflow,
    workflow_from_document,
    workflow_hash,
    write_workflow,
)

TRANSACTION = ".cw/runtime/plan-amend-transaction.json"
_SHA256 = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})")
FailureInjector = Callable[[str], None]
MAX_PLAN_AMENDMENT_BYTES = 1024 * 1024


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
    if metadata.st_size > MAX_PLAN_AMENDMENT_BYTES:
        raise CwError(
            "Plan amendment file is too large", ErrorCode.USAGE_ERROR, exit_code=2,
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


def _strict_amendment_document(text: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object)
    except ValueError as json_error:
        if not isinstance(json_error, json.JSONDecodeError):
            raise CwError(
                "Plan amendment JSON is ambiguous",
                ErrorCode.SCHEMA_VALIDATION_ERROR,
                details=str(json_error),
                exit_code=2,
            ) from json_error
        try:
            import yaml  # type: ignore[import-untyped]
            from yaml.tokens import AliasToken, AnchorToken  # type: ignore[import-untyped]

            if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)):
                raise CwError(
                    "Plan amendment YAML aliases are not allowed",
                    ErrorCode.SCHEMA_VALIDATION_ERROR,
                    exit_code=2,
                )

            class UniqueSafeLoader(yaml.SafeLoader):
                pass

            def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
                mapping: dict[str, Any] = {}
                for key_node, value_node in node.value:
                    key = loader.construct_object(key_node, deep=deep)
                    if key in mapping:
                        raise CwError(
                            f"Plan amendment YAML contains duplicate key: {key}",
                            ErrorCode.SCHEMA_VALIDATION_ERROR,
                            exit_code=2,
                        )
                    mapping[key] = loader.construct_object(value_node, deep=deep)
                return mapping

            UniqueSafeLoader.add_constructor(
                yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping,
            )
            value = yaml.load(text, Loader=UniqueSafeLoader)
        except CwError:
            raise
        except Exception as exc:
            raise CwError(
                "Plan amendment document is invalid",
                ErrorCode.SCHEMA_VALIDATION_ERROR,
                details=str(exc),
                exit_code=2,
            ) from exc
    if not isinstance(value, dict):
        raise CwError(
            "Plan amendment must contain an object",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
            exit_code=2,
        )
    return value


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
    evidence_hashes: dict[str, str] = {}
    if transaction.get("kind") == "plan_artifact_amend":
        manifest_path = backup / "plan-amend-restore-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise CwError(
                "Plan amendment restore manifest is missing",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
        manifest = load_json(manifest_path)
        manifest_evidence = manifest.get("evidence") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "kind", "workflow", "state", "evidence", "proposal_id"}
            or manifest.get("schema_version") != 1
            or manifest.get("kind") != "plan_artifact_amend_restore_manifest"
            or manifest.get("proposal_id") != transaction.get("proposal_id")
            or manifest.get("workflow") != {
                "path": ".codex/workflow/phases.yaml",
                "sha256": transaction.get("previous_workflow_sha256"),
            }
            or manifest.get("state") != {
                "path": ".cw/state.json", "sha256": transaction.get("previous_state_sha256"),
            }
            or not isinstance(manifest_evidence, list)
            or [item.get("path") for item in manifest_evidence if isinstance(item, dict)]
            != transaction.get("superseded_evidence")
        ):
            raise CwError(
                "Plan amendment restore manifest is invalid",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
        evidence_hashes = {
            str(item["path"]): str(item["sha256"])
            for item in manifest_evidence
            if isinstance(item, dict) and set(item) == {"kind", "path", "sha256"}
        }
        if set(evidence_hashes) != set(transaction.get("superseded_evidence", [])):
            raise CwError(
                "Plan amendment restore inventory is invalid",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
    atomic_write_bytes(root / ".codex/workflow/phases.yaml", old_workflow.read_bytes())
    atomic_write_bytes(root / ".cw/state.json", old_state.read_bytes())
    for reference in transaction.get("superseded_evidence", []):
        if not isinstance(reference, str) or not reference.startswith(".cw/"):
            raise CwError(
                "Plan amendment evidence restore target is invalid",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
        source = backup / reference.removeprefix(".cw/")
        target = safe_project_path(root, reference)
        if source.is_symlink() or not source.is_file():
            raise CwError(
                "Plan amendment evidence backup is incomplete",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
        if evidence_hashes.get(reference) != sha256_file(source):
            raise CwError(
                "Plan amendment evidence backup integrity failed",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, source.read_bytes())
    for reference in transaction.get("created_files", []):
        if not isinstance(reference, str) or not reference.startswith(".cw/"):
            raise CwError(
                "Plan amendment rollback target is invalid",
                ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
            )
        target = safe_project_path(root, reference)
        if target.is_file() and not target.is_symlink():
            target.unlink()
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


def _validate_active_transaction(transaction: dict[str, Any]) -> None:
    required = {
        "kind", "created_at", "backup", "previous_workflow_sha256",
        "workflow_sha256", "previous_state_sha256", "superseded_evidence",
        "created_files", "proposal_id", "transaction_sha256",
    }
    superseded = transaction.get("superseded_evidence")
    created = transaction.get("created_files")
    allowed_evidence = re.compile(
        r"\.cw/(?:runtime/(?:READY_FOR_REVIEW|implementer-session)\.json|"
        r"reviews/[A-Za-z0-9._-]+\.json|validation/[A-Za-z0-9._-]+\.json)"
    )
    allowed_created = re.compile(
        r"\.cw/(?:plan-revisions/pr-[0-9a-f]{64}|"
        r"evidence-supersessions/es-[0-9a-f]{64}|"
        r"plan-amendments/pa-[0-9a-f]{64})\.json"
    )
    if (
        set(transaction) != required
        or not isinstance(transaction.get("created_at"), str)
        or re.fullmatch(r"\.cw/backups/[A-Za-z0-9._-]+", str(transaction.get("backup"))) is None
        or any(_SHA256.fullmatch(str(transaction.get(key))) is None for key in (
            "previous_workflow_sha256", "workflow_sha256", "previous_state_sha256",
        ))
        or re.fullmatch(r"pa-[0-9a-f]{64}", str(transaction.get("proposal_id"))) is None
        or not isinstance(superseded, list)
        or len(superseded) != len(set(superseded))
        or any(not isinstance(item, str) or allowed_evidence.fullmatch(item) is None for item in superseded)
        or not isinstance(created, list)
        or len(created) != len(set(created))
        or any(not isinstance(item, str) or allowed_created.fullmatch(item) is None for item in created)
    ):
        raise CwError(
            "Plan amendment transaction schema is invalid",
            ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
        )


def recover_plan_amendment(root: Path) -> bool:
    path = root / TRANSACTION
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise CwError(
            "Plan amendment transaction is unsafe", ErrorCode.PLAN_AMEND_ROLLBACK_FAILED
        )
    transaction = load_json(path)
    if not isinstance(transaction, dict) or transaction.get("kind") not in {
        "plan_amend", "plan_artifact_amend",
    }:
        raise CwError(
            "Plan amendment transaction is invalid",
            ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
        )
    if transaction.get("kind") == "plan_artifact_amend":
        _validate_active_transaction(transaction)
        stored = transaction.get("transaction_sha256")
        body = {key: value for key, value in transaction.items() if key != "transaction_sha256"}
        expected = sha256_bytes(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8"))
        if stored != expected:
            raise CwError(
                "Plan amendment transaction integrity failed",
                ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
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
        proposed_document = _strict_amendment_document(input_bytes.decode("utf-8"))
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


ACTIVE_AMENDMENT_STATES = {
    WorkflowState.IN_PROGRESS,
    WorkflowState.READY_FOR_REVIEW,
    WorkflowState.REVISION_REQUIRED,
}
EVIDENCE_SUPERSESSION_DIRECTORY = ".cw/evidence-supersessions"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _state_hash(root: Path) -> str:
    return sha256_file(root / ".cw/state.json")


def _artifact_path(root: Path, value: str) -> tuple[str, tuple[int, int, int, int, int]]:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or value != value.strip()
        or "\\" in value
        or value.startswith("/")
        or _WINDOWS_DRIVE.match(value)
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise CwError(
            f"Artifact path is not canonical: {value!r}",
            ErrorCode.INVALID_ARTIFACT,
            "Use an existing repository-relative POSIX path.",
            exit_code=2,
        )
    if value.split("/", 1)[0] in {".git", ".cw", ".codex"}:
        raise CwError(
            "Artifact targets protected workflow metadata",
            ErrorCode.INVALID_ARTIFACT,
            exit_code=2,
        )
    try:
        path = safe_project_path(root, value, must_exist=True)
        metadata = path.stat(follow_symlinks=False)
    except (CwError, OSError) as exc:
        raise CwError(
            f"Artifact is missing or unsafe: {value}",
            ErrorCode.INVALID_ARTIFACT,
            details=getattr(exc, "message", str(exc)),
            exit_code=2,
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CwError(
            f"Artifact must be a regular non-linked file: {value}",
            ErrorCode.INVALID_ARTIFACT,
            exit_code=2,
        )
    return value, _file_identity(metadata)


def _covered_by_review_paths(value: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").removeprefix("./")
        if value == normalized or fnmatch.fnmatchcase(value, normalized):
            return True
        if "/**/" in normalized:
            prefix = normalized.split("/**/", 1)[0].rstrip("/")
            if value.startswith(prefix + "/"):
                return True
    return False


def _live_processes(root: Path, workflow: Workflow, phase_id: str) -> None:
    from cw.core.session import load_session, session_path
    from cw.execution.processes import ProcessInspector
    from cw.execution.runs import load_active_run
    from cw.execution.session import active_batch

    inspector = ProcessInspector()
    session = load_session(root, workflow, workflow.phase(phase_id))
    if session is not None and inspector.inspect(session.get("owner_pid")).alive:
        raise CwError("An implementer process is active", ErrorCode.LOCKED, exit_code=3)
    run = load_active_run(root)
    if run is not None:
        if (
            inspector.inspect(run.get("supervisor_pid")).alive
            or inspector.inspect(run.get("process_pid")).alive
        ):
            raise CwError("A managed CW run is active", ErrorCode.LOCKED, exit_code=3)
        raise CwError(
            "An interrupted active run must be recovered before amendment",
            ErrorCode.TRANSACTION_RECOVERY_REQUIRED,
            exit_code=3,
        )
    if active_batch(root) is not None:
        raise CwError("A CW batch is active", ErrorCode.LOCKED, exit_code=3)
    # A stale, structurally valid session is evidence to supersede, not proof
    # that an implementer remains alive. Malformed sessions fail in load_session.
    if session_path(root).exists() and session is None:
        raise CwError("Implementer session is ambiguous", ErrorCode.INVALID_STATE, exit_code=3)


def _evidence_for_phase(
    root: Path,
    workflow: Workflow,
    state: dict[str, Any],
    phase_id: str,
    *,
    allowed_readiness_artifacts: set[str],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    candidates = (
        (root / ".cw/runtime/READY_FOR_REVIEW.json", "readiness"),
        (root / ".cw/runtime/implementer-session.json", "session"),
    )
    for path, kind in candidates:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise CwError("Active phase evidence is unsafe", ErrorCode.SUPERSESSION_INVALID)
        payload = load_json(path)
        if not isinstance(payload, dict) or payload.get("phase") != phase_id:
            raise CwError("Active phase evidence is ambiguous", ErrorCode.SUPERSESSION_INVALID)
        if kind == "readiness":
            allowed = {
                "schema_version", "session_id", "phase", "status",
                "artifacts", "checks_executed",
            }
            old_phase = workflow.phase(phase_id)
            artifact_values = payload.get("artifacts")
            checks = payload.get("checks_executed")
            approved_commands = {item.command for item in old_phase.required_commands}
            if (
                set(payload) - allowed
                or payload.get("schema_version") != 1
                or payload.get("status") != "READY_FOR_REVIEW"
                or re.fullmatch(r"[0-9a-f]{32}", str(payload.get("session_id"))) is None
                or not isinstance(artifact_values, list)
                or not all(isinstance(value, str) for value in artifact_values)
                or not set(artifact_values).issubset(allowed_readiness_artifacts)
                or not isinstance(checks, list)
                or any(
                    not isinstance(check, dict)
                    or set(check) - {"command", "exit_code"}
                    or check.get("command") not in approved_commands
                    or isinstance(check.get("exit_code"), bool)
                    or not isinstance(check.get("exit_code"), int)
                    for check in checks
                )
            ):
                raise CwError("Readiness evidence is invalid", ErrorCode.SUPERSESSION_INVALID)
        evidence.append({
            "kind": kind,
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
        })
    for directory, kind in (("reviews", "review"), ("validation", "validation")):
        parent = root / ".cw" / directory
        for path in sorted(parent.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise CwError("Phase evidence is unsafe", ErrorCode.SUPERSESSION_INVALID)
            payload = load_json(path)
            if not isinstance(payload, dict):
                raise CwError("Phase evidence is invalid", ErrorCode.SUPERSESSION_INVALID)
            if payload.get("phase") == phase_id:
                workflow_id = payload.get("workflow", payload.get("workflow_id"))
                if workflow_id != workflow.id:
                    raise CwError("Phase evidence belongs to another workflow", ErrorCode.SUPERSESSION_INVALID)
                evidence.append({
                    "kind": kind,
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_file(path),
                })
    return evidence


def _proposal_identity(
    phase_id: str,
    artifacts: list[str],
    workflow_sha: str,
    state_sha: str,
    reason: str,
    operator: str,
) -> str:
    payload = {
        "operation": "ADD_PHASE_ARTIFACT",
        "phase": phase_id,
        "added_artifacts": artifacts,
        "expected_workflow_sha256": workflow_sha,
        "expected_state_sha256": state_sha,
        "reason": reason,
        "operator": operator,
    }
    return "pa-" + sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).removeprefix("sha256:")


def _active_preflight(
    root: Path,
    phase_id: str,
    artifacts: list[str],
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    operator: str,
) -> dict[str, Any]:
    if not reason.strip() or len(reason.encode("utf-8")) > 4096:
        raise CwError("Plan amendment requires a reason", ErrorCode.USAGE_ERROR, exit_code=2)
    if not artifacts or len(artifacts) > 100:
        raise CwError("At least one --add-artifact is required", ErrorCode.USAGE_ERROR, exit_code=2)
    workflow_sha = workflow_hash(root / ".codex/workflow/phases.yaml")
    state_sha = _state_hash(root)
    if _normalized_sha256(expected_workflow_sha256) != workflow_sha:
        raise CwError(
            "Workflow changed since the amendment was prepared",
            ErrorCode.STALE_WORKFLOW_SHA,
            "Run cw plan show --json and prepare the amendment again.",
            exit_code=4,
        )
    if _normalized_sha256(expected_state_sha256) != state_sha:
        raise CwError(
            "Workflow state changed since the amendment was prepared",
            ErrorCode.STALE_STATE_SHA,
            "Run cw plan show --json and prepare the amendment again.",
            exit_code=4,
        )
    state = load_state(root)
    workflow = load_workflow(root)
    status = WorkflowState(str(state.get("status")))
    if status not in ACTIVE_AMENDMENT_STATES or workflow.status != "APPROVED":
        raise CwError(
            "Active artifact amendment is not allowed in the current state",
            ErrorCode.INVALID_STATE,
            exit_code=3,
        )
    if state.get("current_phase") != phase_id:
        raise CwError(
            "Only the current active phase can be amended",
            ErrorCode.INVALID_STATE,
            exit_code=3,
        )
    try:
        phase = workflow.phase(phase_id)
    except KeyError as exc:
        raise CwError("Amendment phase does not exist", ErrorCode.INVALID_STATE, exit_code=3) from exc
    from .gates import gate_path
    from .revisions import active_revision, audit_validations

    if gate_path(root, phase_id).exists():
        raise CwError("A completed or gated phase cannot be amended", ErrorCode.INVALID_GATE, exit_code=3)
    validate_state(root, state, workflow)
    audit_history(root, workflow, state)
    audit_validations(root, workflow, state)
    _live_processes(root, workflow, phase_id)
    normalized: list[str] = []
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    declared = {value.casefold(): value for item in workflow.phases for value in item.artifacts}
    for raw in artifacts:
        value, identity = _artifact_path(root, raw)
        folded = value.casefold()
        if folded in declared or folded in {item.casefold() for item in normalized}:
            raise CwError(
                f"Artifact is already declared or collides by case: {value}",
                ErrorCode.INVALID_ARTIFACT,
                exit_code=2,
            )
        if not _covered_by_review_paths(value, phase.review_paths):
            raise CwError(
                f"Artifact is outside the phase review_paths: {value}",
                ErrorCode.INVALID_ARTIFACT,
                exit_code=2,
            )
        normalized.append(value)
        identities[value] = identity
    document = _read_document(root / ".codex/workflow/phases.yaml")
    proposed = copy.deepcopy(document)
    matching = [item for item in proposed.get("phases", []) if item.get("id") == phase_id]
    if len(matching) != 1:
        raise CwError("Amendment phase is ambiguous", ErrorCode.FORBIDDEN_PLAN_CHANGE)
    matching[0].setdefault("artifacts", []).extend(normalized)
    proposed.setdefault("workflow", {})["status"] = "PROPOSED"
    proposed_workflow = workflow_from_document(root, proposed)
    verification = copy.deepcopy(proposed)
    verification["workflow"]["status"] = document["workflow"]["status"]
    verification_phase = next(item for item in verification["phases"] if item["id"] == phase_id)
    verification_phase["artifacts"] = list(next(item for item in document["phases"] if item["id"] == phase_id).get("artifacts", []))
    if verification != document:
        raise CwError(
            "Plan amendment contains a forbidden semantic change",
            ErrorCode.FORBIDDEN_PLAN_CHANGE,
            exit_code=3,
        )
    before_contract, before_contract_sha = _canonical_contract(workflow)
    after_contract, after_contract_sha = _canonical_contract(proposed_workflow)
    if before_contract != after_contract or before_contract_sha != after_contract_sha:
        raise CwError(
            "Completion Contract changes are forbidden",
            ErrorCode.COMPLETION_CONTRACT_CHANGE_REQUIRES_REBUILD,
            exit_code=3,
        )
    old_revision_id, old_revision_sha = active_revision(root, state, workflow)
    evidence = _evidence_for_phase(
        root, workflow, state, phase_id,
        allowed_readiness_artifacts=set(phase.artifacts) | set(normalized),
    )
    proposal = _proposal_identity(
        phase_id, normalized, workflow_sha, state_sha, reason.strip(), operator,
    )
    return {
        "operation": "ADD_PHASE_ARTIFACT",
        "proposal_id": proposal,
        "phase": phase_id,
        "added_artifacts": normalized,
        "removed_artifacts": [],
        "other_changes": [],
        "expected_workflow_sha256": workflow_sha,
        "expected_state_sha256": state_sha,
        "workflow_sha256": sha256_bytes(_serialize_workflow(proposed)),
        "completion_contract_sha256_before": before_contract_sha or "none",
        "completion_contract_sha256_after": after_contract_sha or "none",
        "completion_contract_preserved": True,
        "old_plan_revision_id": old_revision_id,
        "old_plan_revision_sha256": old_revision_sha,
        "evidence": evidence,
        "artifact_identities": identities,
        "document": proposed,
        "state": state,
        "workflow": workflow,
        "proposed_workflow": proposed_workflow,
        "reason": reason.strip(),
        "operator": operator,
        "dry_run": True,
        "approval_required": True,
        "status": WorkflowState.PLAN_PROPOSED.value,
    }


def prepare_active_artifact_amendment(
    root: Path,
    phase_id: str,
    artifacts: list[str],
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    operator: str = "local-operator",
) -> dict[str, Any]:
    """Validate and describe an active artifact amendment without writing."""

    prepared = _active_preflight(
        root, phase_id, artifacts, expected_workflow_sha256,
        expected_state_sha256, reason, operator=operator,
    )
    return {
        key: value for key, value in prepared.items()
        if key not in {"artifact_identities", "document", "state", "workflow", "proposed_workflow", "evidence"}
    } | {"superseded_evidence": [item["path"] for item in prepared["evidence"]]}


def _existing_active_amendment(root: Path, proposal_id: str) -> dict[str, Any] | None:
    directory = root / ".cw/plan-amendments"
    if not directory.is_dir() or directory.is_symlink():
        return None
    path = directory / f"{proposal_id}.json"
    if not path.is_file() or path.is_symlink():
        return None
    payload = load_json(path)
    result = payload.get("result") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "plan_artifact_amendment"
        or payload.get("proposal_id") != proposal_id
        or not isinstance(result, dict)
    ):
        raise CwError("Amendment replay evidence is inconsistent", ErrorCode.OPERATION_CONFLICT)
    return {**result, "idempotent_replay": True}


def _apply_active_artifact_amendment_locked(
    root: Path,
    phase_id: str,
    artifacts: list[str],
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    operator: str = "local-operator",
    failure_injector: FailureInjector | None = None,
) -> dict[str, Any]:
    """Apply a CAS-protected, artifact-addition-only active plan amendment."""

    recover_plan_amendment(root)
    proposal_id = _proposal_identity(
        phase_id, artifacts, _normalized_sha256(expected_workflow_sha256),
        _normalized_sha256(expected_state_sha256), reason.strip(), operator,
    )
    replay = _existing_active_amendment(root, proposal_id)
    if replay is not None:
        return replay
    prepared = _active_preflight(
        root, phase_id, artifacts, expected_workflow_sha256,
        expected_state_sha256, reason, operator=operator,
    )
    backup = backup_metadata(root)
    backup_relative = backup.relative_to(root).as_posix()
    evidence = prepared["evidence"]
    inventory = {
        "schema_version": 1,
        "kind": "plan_artifact_amend_restore_manifest",
        "workflow": {
            "path": ".codex/workflow/phases.yaml",
            "sha256": prepared["expected_workflow_sha256"],
        },
        "state": {"path": ".cw/state.json", "sha256": prepared["expected_state_sha256"]},
        "evidence": evidence,
        "proposal_id": prepared["proposal_id"],
    }
    atomic_json(backup / "plan-amend-restore-manifest.json", inventory)
    # Second CAS and file-identity check occur after backup and immediately
    # before the journal becomes authoritative.
    if (
        workflow_hash(root / ".codex/workflow/phases.yaml") != prepared["expected_workflow_sha256"]
        or _state_hash(root) != prepared["expected_state_sha256"]
    ):
        raise CwError("Workflow or state changed during amendment", ErrorCode.OPERATION_CONFLICT, exit_code=4)
    for value, identity in prepared["artifact_identities"].items():
        _, observed = _artifact_path(root, value)
        if observed != identity:
            raise CwError("Artifact changed during amendment", ErrorCode.OPERATION_CONFLICT, exit_code=4)

    from .revisions import (
        load_revision,
        persist_revision,
        revision_path,
        revision_payload,
    )

    old_id = prepared["old_plan_revision_id"]
    old_revision = (
        load_revision(root, old_id)
        if revision_path(root, old_id).exists()
        else revision_payload(
            root, _read_document(root / ".codex/workflow/phases.yaml"),
            parent_revision_id=None, actor_id="legacy-migration",
            actor_origin="internal_supervisor",
        )
    )
    new_revision = revision_payload(
        root, prepared["document"], parent_revision_id=old_id,
        actor_id=operator, actor_origin="human_cli",
        authorization_reference=None,
    )
    created_at = utc_now()
    result = {
        "amended": True,
        "operation": "ADD_PHASE_ARTIFACT",
        "proposal_id": prepared["proposal_id"],
        "phase": phase_id,
        "added_artifacts": prepared["added_artifacts"],
        "removed_artifacts": [],
        "other_changes": [],
        "previous_workflow_sha256": prepared["expected_workflow_sha256"],
        "previous_state_sha256": prepared["expected_state_sha256"],
        "workflow_sha256": prepared["workflow_sha256"],
        "completion_contract_sha256_before": prepared["completion_contract_sha256_before"],
        "completion_contract_sha256_after": prepared["completion_contract_sha256_after"],
        "completion_contract_preserved": True,
        "previous_plan_revision": old_id,
        "new_plan_revision": new_revision["plan_revision_id"],
        "backup": backup_relative,
        "status": WorkflowState.PLAN_PROPOSED.value,
        "approval_required": True,
        "automatic_approval": False,
        "dry_run": False,
        "superseded_evidence": [item["path"] for item in evidence],
    }
    operation_record = {
        "schema_version": 1,
        "kind": "plan_artifact_amendment",
        "proposal_id": prepared["proposal_id"],
        "project": prepared["workflow"].id,
        "phase": phase_id,
        "reason": prepared["reason"],
        "operator": operator,
        "created_at": created_at,
        "result": result,
    }
    operation_directory = safe_directory(
        root / ".cw/plan-amendments", ".cw/plan-amendments", create=True,
    )
    operation_path = operation_directory / f"{prepared['proposal_id']}.json"
    supersession_payloads: list[tuple[Path, dict[str, Any]]] = []
    directory = safe_directory(
        root / EVIDENCE_SUPERSESSION_DIRECTORY,
        EVIDENCE_SUPERSESSION_DIRECTORY,
        create=True,
    )
    for item in evidence:
        body = {
            "schema_version": 1,
            "kind": "phase_evidence_supersession",
            "project": prepared["workflow"].id,
            "phase": phase_id,
            "previous_plan_revision": old_id,
            "new_plan_revision": new_revision["plan_revision_id"],
            "original_path": item["path"],
            "original_sha256": item["sha256"],
            "backup_path": f"{backup_relative}/{item['path'].removeprefix('.cw/')}",
            "evidence_kind": item["kind"],
            "reason": prepared["reason"],
            "operator": operator,
            "created_at": created_at,
            "proposal_id": prepared["proposal_id"],
            "result": result,
        }
        identifier = "es-" + sha256_bytes(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).removeprefix("sha256:")
        body["supersession_id"] = identifier
        path = directory / f"{identifier}.json"
        supersession_payloads.append((path, body))
    created_files = [operation_path.relative_to(root).as_posix()] + [
        path.relative_to(root).as_posix()
        for path in (revision_path(root, old_id), revision_path(root, new_revision["plan_revision_id"]))
        if not path.exists()
    ] + [path.relative_to(root).as_posix() for path, _ in supersession_payloads]
    transaction = {
        "kind": "plan_artifact_amend",
        "created_at": created_at,
        "backup": backup_relative,
        "previous_workflow_sha256": prepared["expected_workflow_sha256"],
        "workflow_sha256": prepared["workflow_sha256"],
        "previous_state_sha256": prepared["expected_state_sha256"],
        "superseded_evidence": [item["path"] for item in evidence],
        "created_files": created_files,
        "proposal_id": prepared["proposal_id"],
    }
    transaction["transaction_sha256"] = sha256_bytes(json.dumps(
        transaction, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8"))
    transaction_path = root / TRANSACTION
    atomic_json(transaction_path, transaction)

    def step(name: str) -> None:
        if failure_injector is not None:
            failure_injector(name)

    try:
        persist_revision(root, old_revision)
        step("old_revision_persisted")
        persist_revision(root, new_revision)
        step("new_revision_persisted")
        from .utils import atomic_json_new

        atomic_json_new(operation_path, operation_record)
        step("operation_record_persisted")
        for target, payload in supersession_payloads:
            atomic_json_new(target, payload)
        step("supersessions_persisted")
        for item in evidence:
            safe_project_path(root, item["path"], must_exist=True).unlink()
        step("active_evidence_removed")
        write_workflow(root / ".codex/workflow/phases.yaml", prepared["document"])
        step("workflow_activated")
        state = copy.deepcopy(prepared["state"])
        state.update({
            "workflow_version": prepared["proposed_workflow"].version,
            "workflow_sha256": prepared["workflow_sha256"],
            "active_plan_revision": new_revision["plan_revision_id"],
            "active_plan_revision_sha256": new_revision["canonical_workflow_sha256"],
            "superseded_plan_revisions": [
                *[item for item in state.get("superseded_plan_revisions", []) if isinstance(item, str)],
                old_id,
            ],
            "status": WorkflowState.PLAN_PROPOSED.value,
            "attempt": 0,
            "revision_attempt": 0,
            "last_review": None,
            "last_error": None,
            "infrastructure_error": None,
            "pending_rebaseline": None,
        })
        state.setdefault("history", []).append({
            "timestamp": created_at,
            "phase": phase_id,
            "action": "phase_artifacts_amended",
            "proposal_id": prepared["proposal_id"],
            "previous_workflow_sha256": prepared["expected_workflow_sha256"],
            "workflow_sha256": prepared["workflow_sha256"],
            "previous_state_sha256": prepared["expected_state_sha256"],
            "completion_contract_sha256": prepared["completion_contract_sha256_before"],
            "previous_plan_revision": old_id,
            "new_plan_revision": new_revision["plan_revision_id"],
            "added_artifacts": prepared["added_artifacts"],
            "superseded_evidence": [item["path"] for item in evidence],
            "backup": backup_relative,
            "reason": prepared["reason"],
            "operator": operator,
        })
        save_state(root, state)
        step("state_activated")
        reloaded_workflow = load_workflow(root)
        reloaded_state = load_state(root)
        validate_state(root, reloaded_state, reloaded_workflow)
        audit_history(root, reloaded_workflow, reloaded_state)
        audit_evidence_supersessions(root, reloaded_workflow, reloaded_state)
        step("audit_completed")
    except BaseException as exc:
        try:
            _restore(root, transaction)
            _remove_transaction(transaction_path)
        except Exception as rollback_exc:
            raise CwError(
                "Plan amendment rollback failed", ErrorCode.PLAN_AMEND_ROLLBACK_FAILED,
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
    return result


def apply_active_artifact_amendment(
    root: Path,
    phase_id: str,
    artifacts: list[str],
    expected_workflow_sha256: str,
    expected_state_sha256: str,
    reason: str,
    *,
    operator: str = "local-operator",
    failure_injector: FailureInjector | None = None,
) -> dict[str, Any]:
    """Acquire the exclusive project lock and apply an active amendment."""

    from .locking import operation_lock

    with operation_lock(root, "plan-amend"):
        return _apply_active_artifact_amendment_locked(
            root, phase_id, artifacts, expected_workflow_sha256,
            expected_state_sha256, reason, operator=operator,
            failure_injector=failure_injector,
        )


def audit_evidence_supersessions(
    root: Path, workflow: Workflow, state: dict[str, Any]
) -> int:
    """Validate append-only evidence moved out of the active namespace."""

    operations = root / ".cw/plan-amendments"
    if operations.exists():
        if operations.is_symlink() or not operations.is_dir():
            raise CwError("Plan amendment history is unsafe", ErrorCode.SUPERSESSION_INVALID)
        for path in sorted(operations.iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"pa-[0-9a-f]{64}\.json", path.name):
                raise CwError("Unexpected plan amendment history artifact", ErrorCode.SUPERSESSION_INVALID)
            payload = load_json(path)
            if (
                not isinstance(payload, dict)
                or set(payload) != {
                    "schema_version", "kind", "proposal_id", "project", "phase",
                    "reason", "operator", "created_at", "result",
                }
                or payload.get("schema_version") != 1
                or payload.get("kind") != "plan_artifact_amendment"
                or payload.get("proposal_id") != path.stem
                or payload.get("project") != workflow.id
                or payload.get("phase") not in {phase.id for phase in workflow.phases}
                or not isinstance(payload.get("reason"), str)
                or not payload["reason"].strip()
                or not isinstance(payload.get("operator"), str)
                or not isinstance(payload.get("result"), dict)
                or payload["result"].get("proposal_id") != path.stem
            ):
                raise CwError("Plan amendment history is invalid", ErrorCode.SUPERSESSION_INVALID)
            result = payload["result"]
            expected_proposal = _proposal_identity(
                str(payload["phase"]), list(result.get("added_artifacts", [])),
                str(result.get("previous_workflow_sha256")),
                str(result.get("previous_state_sha256")), str(payload["reason"]),
                str(payload["operator"]),
            )
            if expected_proposal != path.stem:
                raise CwError("Plan amendment history hash is invalid", ErrorCode.SUPERSESSION_INVALID)
    directory = root / EVIDENCE_SUPERSESSION_DIRECTORY
    if not directory.exists():
        return 0
    if directory.is_symlink() or not directory.is_dir():
        raise CwError("Evidence supersession directory is unsafe", ErrorCode.SUPERSESSION_INVALID)
    seen: set[str] = set()
    historical = set(state.get("superseded_plan_revisions", []))
    count = 0
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"es-[0-9a-f]{64}\.json", path.name):
            raise CwError("Unexpected evidence supersession artifact", ErrorCode.SUPERSESSION_INVALID)
        payload = load_json(path)
        required = {
            "schema_version", "kind", "project", "phase", "previous_plan_revision",
            "new_plan_revision", "original_path", "original_sha256", "backup_path",
            "evidence_kind", "reason", "operator", "created_at", "proposal_id",
            "result", "supersession_id",
        }
        original = payload.get("original_path") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload.get("schema_version") != 1
            or payload.get("kind") != "phase_evidence_supersession"
            or payload.get("project") != workflow.id
            or payload.get("phase") not in {phase.id for phase in workflow.phases}
            or payload.get("previous_plan_revision") not in historical
            or payload.get("new_plan_revision") not in historical | {state.get("active_plan_revision")}
            or not isinstance(original, str)
            or original in seen
            or not original.startswith(".cw/")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("original_sha256")))
            or not isinstance(payload.get("reason"), str)
            or not payload["reason"].strip()
            or not isinstance(payload.get("operator"), str)
            or not payload["operator"]
            or payload.get("supersession_id") != path.stem
        ):
            raise CwError("Evidence supersession is invalid", ErrorCode.SUPERSESSION_INVALID)
        body = {key: value for key, value in payload.items() if key != "supersession_id"}
        expected_identifier = "es-" + sha256_bytes(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).removeprefix("sha256:")
        if expected_identifier != path.stem:
            raise CwError("Evidence supersession hash is invalid", ErrorCode.SUPERSESSION_INVALID)
        if not str(payload.get("backup_path", "")).startswith(".cw/backups/"):
            raise CwError("Evidence supersession backup is invalid", ErrorCode.SUPERSESSION_INVALID)
        backup_path = safe_project_path(root, str(payload.get("backup_path")), must_exist=True)
        if backup_path.is_symlink() or not backup_path.is_file() or sha256_file(backup_path) != payload["original_sha256"]:
            raise CwError("Superseded evidence backup hash is invalid", ErrorCode.SUPERSESSION_INVALID)
        if safe_project_path(root, original).exists():
            raise CwError("Superseded evidence remains active", ErrorCode.SUPERSESSION_INVALID)
        seen.add(original)
        count += 1
    return count
