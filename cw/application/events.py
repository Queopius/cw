from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from cw.core.utils import utc_now


class ApplicationEventType(str, Enum):
    PROJECT_INITIALIZED = "PROJECT_INITIALIZED"
    PHASE_STARTED = "PHASE_STARTED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    GATE_APPROVED = "GATE_APPROVED"
    COMPLETION_REVIEW_COMPLETED = "COMPLETION_REVIEW_COMPLETED"
    EXTENSION_PROPOSED = "EXTENSION_PROPOSED"
    EXTENSION_AUTHORIZED = "EXTENSION_AUTHORIZED"
    PROJECT_COMPLETED = "PROJECT_COMPLETED"


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    event_type: ApplicationEventType
    project_id: str
    operation_id: str
    data: dict[str, Any]
    occurred_at: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type.value,
            "project_id": self.project_id,
            "operation_id": self.operation_id,
            "occurred_at": self.occurred_at or utc_now(),
            "data": self.data,
        }
