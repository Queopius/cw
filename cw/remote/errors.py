from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RemoteErrorCode(str, Enum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    SCOPE_REQUIRED = "SCOPE_REQUIRED"
    DEVICE_NOT_PAIRED = "DEVICE_NOT_PAIRED"
    DEVICE_REVOKED = "DEVICE_REVOKED"
    AGENT_OFFLINE = "AGENT_OFFLINE"
    PROJECT_NOT_GRANTED = "PROJECT_NOT_GRANTED"
    PROJECT_SCOPE_VIOLATION = "PROJECT_SCOPE_VIOLATION"
    PLATFORM_CAPABILITY_UNAVAILABLE = "PLATFORM_CAPABILITY_UNAVAILABLE"
    OPERATION_CONFLICT = "OPERATION_CONFLICT"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    REMOTE_TRANSPORT_UNAVAILABLE = "REMOTE_TRANSPORT_UNAVAILABLE"
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    INVALID_REQUEST = "INVALID_REQUEST"
    RATE_LIMITED = "RATE_LIMITED"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class RemoteError(RuntimeError):
    code: RemoteErrorCode
    message: str
    retryable: bool = False
    http_status: int = 400
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
