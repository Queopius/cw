from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkflowState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    PLANNING = "PLANNING"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REVIEWING = "REVIEWING"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    APPROVED = "APPROVED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    description: str
    severity: str = "blocking"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Criterion":
        return cls(str(data["id"]), str(data["description"]), str(data.get("severity", "blocking")))


@dataclass(frozen=True, slots=True)
class RequiredCommand:
    command: str
    timeout_seconds: int | None = None

    @classmethod
    def from_value(cls, value: Any) -> "RequiredCommand":
        if isinstance(value, str):
            return cls(value)
        return cls(str(value["command"]), int(value["timeout_seconds"]) if value.get("timeout_seconds") else None)


@dataclass(frozen=True, slots=True)
class Phase:
    id: str
    name: str
    objective: str
    depends_on: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    review_paths: tuple[str, ...] = ()
    required_commands: tuple[RequiredCommand, ...] = ()
    acceptance_criteria: tuple[Criterion, ...] = ()
    blocking_criteria: tuple[str, ...] = ()
    requires_human_approval: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase":
        return cls(
            id=str(data["id"]), name=str(data["name"]),
            objective=str(data.get("objective", data["name"])),
            depends_on=tuple(map(str, data.get("depends_on", []))),
            artifacts=tuple(map(str, data.get("artifacts", []))),
            review_paths=tuple(map(str, data.get("review_paths", []))),
            required_commands=tuple(RequiredCommand.from_value(v) for v in data.get("required_commands", [])),
            acceptance_criteria=tuple(Criterion.from_dict(v) for v in data.get("acceptance_criteria", [])),
            blocking_criteria=tuple(map(str, data.get("blocking_criteria", []))),
            requires_human_approval=bool(data.get("requires_human_approval", False)),
        )


@dataclass(frozen=True, slots=True)
class Workflow:
    id: str
    repository: str
    version: int
    status: str
    goal: str | None
    phases: tuple[Phase, ...]
    max_review_attempts: int = 3
    command_timeout: int = 1200
    review_timeout: int = 1200

    def phase(self, phase_id: str) -> Phase:
        for phase in self.phases:
            if phase.id == phase_id:
                return phase
        raise KeyError(phase_id)

    def index(self, phase_id: str) -> int:
        return next(i for i, phase in enumerate(self.phases) if phase.id == phase_id)


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
