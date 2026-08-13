from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class IntegrationHealth(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class Requirement(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    UNUSED = "UNUSED"


@dataclass(frozen=True, slots=True)
class IntegrationDiagnostic:
    integration: str
    status: IntegrationHealth
    error_code: str
    http_status: int | None = None
    occurrences: int = 1
    summary: str = ""
    required: bool = False

    @property
    def impact(self) -> str:
        return "BLOCKING" if self.required else "NONE"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["impact"] = self.impact
        return value


@dataclass(frozen=True, slots=True)
class Integration:
    id: str
    type: str
    enabled: bool
    required: Requirement
    health: IntegrationHealth
    source: str = "codex"
    capability: str = "mcp"
    error_code: str | None = None
    http_status: int | None = None
    occurrences: int = 0

    @property
    def impact(self) -> str:
        if self.required is Requirement.REQUIRED and self.health is not IntegrationHealth.AVAILABLE:
            return "BLOCKING"
        return "NONE"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required"] = self.required.value
        value["health"] = self.health.value
        value["impact"] = self.impact
        return value
