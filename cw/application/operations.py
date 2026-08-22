from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cw.core.authorization import OperationContext
from cw.core.diagnostics import redact
from cw.core.errors import CwError
from cw.core.layout import safe_directory, safe_file
from cw.core.platform import process_is_alive
from cw.core.utils import atomic_json, atomic_json_new, load_json, utc_now

from .models import (
    ApplicationError,
    ApplicationErrorCode,
    OperationResult,
    OperationStatus,
    application_error,
)
from .projects import ResolvedProject


OperationExecutor = Callable[[], dict[str, Any]]
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TERMINAL = {
    OperationStatus.SUCCEEDED.value,
    OperationStatus.FAILED.value,
    OperationStatus.BLOCKED.value,
    OperationStatus.CANCELLED.value,
}


def _elapsed(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((datetime.now(timezone.utc) - started).total_seconds(), 3))


class OperationSupervisor:
    """Persistent application operation lifecycle shared by non-CLI adapters.

    The durable records are recovery metadata, not a second workflow database.
    Workflow state and evidence under ``.cw`` remain authoritative.
    """

    def __init__(self, *, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cw-operation",
        )
        self._guard = threading.RLock()
        self._futures: dict[tuple[str, str], Future[None]] = {}

    @staticmethod
    def _directory(project: ResolvedProject) -> Path:
        runtime = safe_directory(project.root / ".cw" / "runtime", ".cw/runtime", create=True)
        return safe_directory(runtime / "operations", ".cw/runtime/operations", create=True)

    @classmethod
    def _path(cls, project: ResolvedProject, operation_id: str) -> Path:
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise ApplicationError(
                ApplicationErrorCode.INVALID_REQUEST,
                "operation_id must be 1-128 safe identifier characters",
            )
        # Operation identifiers are protocol values, not filesystem names.
        # Hashing keeps Windows-reserved characters and path disclosure out of
        # the durable runtime layout.
        name = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return cls._directory(project) / f"{name}.json"

    @staticmethod
    def _digest(operation: str, capability: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            {"operation": operation, "capability": capability, "payload": payload},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _load(cls, project: ResolvedProject, operation_id: str) -> dict[str, Any]:
        path = safe_file(cls._path(project, operation_id), "CW operation record")
        if not path.is_file():
            raise ApplicationError(
                ApplicationErrorCode.OPERATION_NOT_FOUND,
                "The operation does not exist for this project",
            )
        record = load_json(path)
        fields = {
            "schema_version", "operation_id", "project_id", "operation", "capability",
            "actor_origin", "request_digest", "status", "stage", "phase", "created_at",
            "started_at", "finished_at", "elapsed_seconds", "supervisor_pid",
            "cancellation_requested", "result", "error", "updated_at",
        }
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or record.get("schema_version") != 1
            or record.get("operation_id") != operation_id
            or record.get("project_id") != project.handle.repository_id
            or record.get("status") not in {item.value for item in OperationStatus}
            or not isinstance(record.get("operation"), str)
            or not isinstance(record.get("capability"), str)
            or not isinstance(record.get("actor_origin"), str)
            or not isinstance(record.get("request_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["request_digest"]) is None
            or not isinstance(record.get("stage"), str)
            or record.get("phase") is not None and not isinstance(record.get("phase"), str)
            or not isinstance(record.get("created_at"), str)
            or not isinstance(record.get("updated_at"), str)
            or record.get("started_at") is not None and not isinstance(record.get("started_at"), str)
            or record.get("finished_at") is not None and not isinstance(record.get("finished_at"), str)
            or record.get("elapsed_seconds") is not None and not isinstance(record.get("elapsed_seconds"), (int, float))
            or isinstance(record.get("supervisor_pid"), bool)
            or not isinstance(record.get("supervisor_pid"), int)
            or not isinstance(record.get("cancellation_requested"), bool)
            or record.get("result") is not None and not isinstance(record.get("result"), dict)
            or record.get("error") is not None and not isinstance(record.get("error"), dict)
        ):
            raise ApplicationError(
                ApplicationErrorCode.STATE_INCONSISTENT,
                "The operation record is invalid",
            )
        return record

    @classmethod
    def _write(cls, project: ResolvedProject, record: dict[str, Any]) -> None:
        record["updated_at"] = utc_now()
        atomic_json(cls._path(project, str(record["operation_id"])), record)

    @staticmethod
    def _result(record: dict[str, Any], *, replay: bool = False) -> OperationResult:
        status = OperationStatus(str(record["status"]))
        data = {
            "stage": record.get("stage"),
            "phase": record.get("phase"),
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "elapsed_seconds": (
                record.get("elapsed_seconds")
                if record.get("elapsed_seconds") is not None
                else _elapsed(record.get("started_at"))
            ),
            "result": record.get("result"),
            "error": record.get("error"),
            "cancellation_requested": bool(record.get("cancellation_requested")),
        }
        return OperationResult(
            operation_id=str(record["operation_id"]),
            operation=str(record["operation"]),
            capability=str(record["capability"]),
            project_id=str(record["project_id"]),
            status=status,
            data=data,
            idempotent_replay=replay,
            actor_origin=str(record["actor_origin"]),
        )

    def submit(
        self,
        project: ResolvedProject,
        request: OperationContext,
        *,
        operation: str,
        capability: str,
        phase: str | None,
        payload: dict[str, Any],
        executor: OperationExecutor,
    ) -> OperationResult:
        digest = self._digest(operation, capability, payload)
        created_at = utc_now()
        record = {
            "schema_version": 1,
            "operation_id": request.operation_id,
            "project_id": project.handle.repository_id,
            "operation": operation,
            "capability": capability,
            "actor_origin": request.actor.origin.value,
            "request_digest": digest,
            "status": OperationStatus.QUEUED.value,
            "stage": "queued",
            "phase": phase,
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": None,
            "supervisor_pid": os.getpid(),
            "cancellation_requested": False,
            "result": None,
            "error": None,
            "updated_at": created_at,
        }
        key = (project.handle.repository_id, request.operation_id)
        with self._guard:
            path = self._path(project, request.operation_id)
            try:
                atomic_json_new(path, record)
            except FileExistsError:
                existing = self._load(project, request.operation_id)
                if (
                    existing.get("request_digest") != digest
                    or existing.get("operation") != operation
                    or existing.get("capability") != capability
                    or existing.get("actor_origin") != request.actor.origin.value
                ):
                    raise ApplicationError(
                        ApplicationErrorCode.OPERATION_CONFLICT,
                        "operation_id was already used for a different request",
                    )
                return self._result(existing, replay=True)
            self._futures[key] = self._executor.submit(
                self._run, project, request.operation_id, executor,
            )
        return self._result(record)

    def _run(
        self, project: ResolvedProject, operation_id: str, executor: OperationExecutor,
    ) -> None:
        key = (project.handle.repository_id, operation_id)
        started_at = utc_now()
        try:
            with self._guard:
                record = self._load(project, operation_id)
                if record["status"] == OperationStatus.CANCELLED.value:
                    return
                stage = {
                    "phase.start": "phase_start",
                    "validation.run": "validation_execution",
                    "review.request": "reviewer_execution",
                    "operation.retry": "retry_execution",
                }.get(str(record.get("operation")), "executing")
                record.update({
                    "status": OperationStatus.RUNNING.value,
                    "stage": stage,
                    "started_at": started_at,
                })
                self._write(project, record)
            result = executor()
            status = OperationStatus.SUCCEEDED
            error = None
        except ApplicationError as exc:
            result = None
            status = (
                OperationStatus.BLOCKED
                if exc.retryable or exc.code is ApplicationErrorCode.INFRASTRUCTURE_FAILURE
                else OperationStatus.FAILED
            )
            error = {
                "code": exc.code.value,
                "message": redact(exc.message),
                "retryable": exc.retryable,
                "details": exc.details,
            }
        except CwError as exc:
            mapped = application_error(exc)
            result = None
            status = OperationStatus.BLOCKED if mapped.retryable else OperationStatus.FAILED
            error = {
                "code": mapped.code.value,
                "message": redact(mapped.message),
                "retryable": mapped.retryable,
                "details": mapped.details,
            }
        except Exception:
            result = None
            status = OperationStatus.BLOCKED
            error = {
                "code": ApplicationErrorCode.INFRASTRUCTURE_FAILURE.value,
                "message": "CW could not complete the controlled operation",
                "retryable": True,
                "details": {},
            }
        finally:
            finished_at = utc_now()
            with self._guard:
                try:
                    record = self._load(project, operation_id)
                    if record["status"] != OperationStatus.CANCELLED.value:
                        record.update({
                            "status": status.value,
                            "stage": "completed" if status is OperationStatus.SUCCEEDED else status.value.lower(),
                            "finished_at": finished_at,
                            "elapsed_seconds": _elapsed(started_at),
                            "result": result,
                            "error": error,
                        })
                        self._write(project, record)
                finally:
                    self._futures.pop(key, None)

    def status(self, project: ResolvedProject, operation_id: str) -> OperationResult:
        record = self._load(project, operation_id)
        if record["status"] in {OperationStatus.QUEUED.value, OperationStatus.RUNNING.value}:
            owner = record.get("supervisor_pid")
            if isinstance(owner, int) and not process_is_alive(owner):
                record.update({
                    "status": OperationStatus.BLOCKED.value,
                    "stage": "recovery_required",
                    "finished_at": utc_now(),
                    "error": {
                        "code": ApplicationErrorCode.INFRASTRUCTURE_FAILURE.value,
                        "message": "The operation supervisor stopped; reconcile CW state before retrying",
                        "retryable": True,
                        "details": {},
                    },
                })
                self._write(project, record)
        return self._result(record)

    def cancel(self, project: ResolvedProject, operation_id: str) -> OperationResult:
        key = (project.handle.repository_id, operation_id)
        with self._guard:
            record = self._load(project, operation_id)
            if record["status"] == OperationStatus.CANCELLED.value:
                return self._result(record, replay=True)
            if record["status"] in _TERMINAL:
                raise ApplicationError(
                    ApplicationErrorCode.OPERATION_CONFLICT,
                    "A completed operation cannot be cancelled",
                )
            future = self._futures.get(key)
            if record["status"] != OperationStatus.QUEUED.value or future is None or not future.cancel():
                raise ApplicationError(
                    ApplicationErrorCode.OPERATION_IN_PROGRESS,
                    "The operation is already running and cannot be cancelled safely",
                    retryable=False,
                )
            record.update({
                "status": OperationStatus.CANCELLED.value,
                "stage": "cancelled_before_execution",
                "finished_at": utc_now(),
                "cancellation_requested": True,
                "error": {
                    "code": ApplicationErrorCode.OPERATION_CANCELLED.value,
                    "message": "Operation cancelled before execution",
                    "retryable": False,
                    "details": {},
                },
            })
            self._write(project, record)
            self._futures.pop(key, None)
            return self._result(record)

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)
