from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from cw.core.errors import CwError, ErrorCode


class ApplicationErrorCode(str, Enum):
    PROJECT_NOT_INITIALIZED = "PROJECT_NOT_INITIALIZED"
    PROJECT_SCOPE_VIOLATION = "PROJECT_SCOPE_VIOLATION"
    PROJECT_COMPLETED = "PROJECT_COMPLETED"
    PHASE_NOT_ACTIVE = "PHASE_NOT_ACTIVE"
    PHASE_NOT_STARTABLE = "PHASE_NOT_STARTABLE"
    INVALID_GATE = "INVALID_GATE"
    REVIEW_IN_PROGRESS = "REVIEW_IN_PROGRESS"
    VALIDATION_IN_PROGRESS = "VALIDATION_IN_PROGRESS"
    STATE_INCONSISTENT = "STATE_INCONSISTENT"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    EXTENSION_NOT_PROPOSED = "EXTENSION_NOT_PROPOSED"
    OPERATION_CONFLICT = "OPERATION_CONFLICT"
    OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    OPERATION_CANCELLED = "OPERATION_CANCELLED"
    RETRY_NOT_ALLOWED = "RETRY_NOT_ALLOWED"
    COMPLETION_EXTENSION_PENDING = "COMPLETION_EXTENSION_PENDING"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    INVALID_REQUEST = "INVALID_REQUEST"


@dataclass(slots=True)
class ApplicationError(RuntimeError):
    code: ApplicationErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class OperationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation_id: str
    operation: str
    capability: str
    project_id: str
    status: OperationStatus
    data: dict[str, Any]
    schema_version: int = 1
    idempotent_replay: bool = False
    actor_origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "operation": self.operation,
            "capability": self.capability,
            "project_id": self.project_id,
            "status": self.status.value,
            "idempotent_replay": self.idempotent_replay,
            "actor_origin": self.actor_origin,
            "data": self.data,
        }


_APPLICATION_ERROR_MAP = {
    ErrorCode.AUTHORIZATION_REQUIRED: ApplicationErrorCode.AUTHORIZATION_REQUIRED,
    ErrorCode.OPERATION_CONFLICT: ApplicationErrorCode.OPERATION_CONFLICT,
    ErrorCode.PROJECT_SCOPE_VIOLATION: ApplicationErrorCode.PROJECT_SCOPE_VIOLATION,
    ErrorCode.INVALID_GATE: ApplicationErrorCode.INVALID_GATE,
    ErrorCode.STATE_INCONSISTENT: ApplicationErrorCode.STATE_INCONSISTENT,
    ErrorCode.LOCKED: ApplicationErrorCode.OPERATION_CONFLICT,
}


def application_error(error: CwError) -> ApplicationError:
    code = _APPLICATION_ERROR_MAP.get(error.code)
    if code is None:
        if error.code in {
            ErrorCode.REVIEWER_NETWORK_ERROR, ErrorCode.REVIEWER_PROCESS_ERROR,
            ErrorCode.IMPLEMENTER_PROCESS_ERROR, ErrorCode.PLANNER_NETWORK_ERROR,
            ErrorCode.PLANNER_PROCESS_ERROR, ErrorCode.CODEX_NOT_FOUND,
            ErrorCode.REVIEW_TIMEOUT, ErrorCode.EXECUTION_INTERRUPTED,
        }:
            code = ApplicationErrorCode.INFRASTRUCTURE_FAILURE
        elif error.code is ErrorCode.INVALID_STATE and "initialized" in error.message.lower():
            code = ApplicationErrorCode.PROJECT_NOT_INITIALIZED
        elif error.code is ErrorCode.INVALID_STATE and "extension proposal" in error.message.lower():
            code = ApplicationErrorCode.EXTENSION_NOT_PROPOSED
        elif error.code is ErrorCode.INVALID_STATE:
            code = ApplicationErrorCode.STATE_INCONSISTENT
        else:
            code = ApplicationErrorCode.INVALID_REQUEST
    return ApplicationError(
        code,
        error.message,
        retryable=error.code in {
            ErrorCode.REVIEWER_NETWORK_ERROR, ErrorCode.REVIEWER_PROCESS_ERROR,
            ErrorCode.PLANNER_NETWORK_ERROR, ErrorCode.PLANNER_PROCESS_ERROR,
            ErrorCode.LOCKED,
        },
        details={"cw_error_code": error.code.value, "hint": error.hint},
    )
