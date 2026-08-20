from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .severity import CriterionSeverity


class WorkflowState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZED = "INITIALIZED"
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
    PLANNED_COMPLETE = "PLANNED_COMPLETE"
    COMPLETION_REVIEW = "COMPLETION_REVIEW"
    EXTENSION_PROPOSED = "EXTENSION_PROPOSED"
    COMPLETION_BLOCKED = "COMPLETION_BLOCKED"
    COMPLETED = "COMPLETED"


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class PlanStatus(str, Enum):
    """Public lifecycle values persisted in the static workflow document."""

    NOT_CREATED = "NOT_CREATED"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"


class CompletionDecision(str, Enum):
    SATISFIED = "SATISFIED"
    EXTENSION_REQUIRED = "EXTENSION_REQUIRED"
    BLOCKED = "BLOCKED"


class CompletionResultStatus(str, Enum):
    VERIFIED = "VERIFIED"
    INFERRED = "INFERRED"
    NOT_VERIFIED = "NOT_VERIFIED"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    description: str
    severity: CriterionSeverity = CriterionSeverity.BLOCKING

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Criterion":
        return cls(
            str(data["id"]),
            str(data["description"]),
            CriterionSeverity(str(data.get("severity", CriterionSeverity.BLOCKING.value))),
        )


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
    required_integrations: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    completion_requirements: tuple[str, ...] = ()

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
            required_integrations=tuple(map(str, data.get("required_integrations", []))),
            expected_evidence=tuple(map(str, data.get("expected_evidence", []))),
            completion_requirements=tuple(map(str, data.get("completion_requirements", []))),
        )


@dataclass(frozen=True, slots=True)
class CompletionRequirement:
    id: str
    description: str
    severity: CriterionSeverity = CriterionSeverity.BLOCKING
    evidence_expectations: tuple[str, ...] = ()
    project_specific: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionRequirement":
        return cls(
            id=str(data["id"]),
            description=str(data["description"]),
            severity=CriterionSeverity(str(data.get("severity", CriterionSeverity.BLOCKING.value))),
            evidence_expectations=tuple(map(str, data.get("evidence_expectations", []))),
            project_specific=bool(data.get("project_specific", False)),
        )


@dataclass(frozen=True, slots=True)
class CompletionContract:
    id: str
    name: str
    description: str
    target_type: str
    requirements: tuple[CompletionRequirement, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionContract":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            description=str(data["description"]),
            target_type=str(data["target_type"]),
            requirements=tuple(CompletionRequirement.from_dict(item) for item in data.get("requirements", [])),
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
    allow_network: bool = False
    protected_paths: tuple[str, ...] = ()
    human_gate_categories: tuple[str, ...] = ()
    completion_target: CompletionContract | None = None

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
