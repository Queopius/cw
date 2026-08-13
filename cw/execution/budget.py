from __future__ import annotations

from dataclasses import dataclass

from cw.core.errors import CwError, ErrorCode


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    max_phases: int = 1
    max_wall_time_seconds: int = 7200
    max_semantic_revisions_per_phase: int = 3
    max_agent_runs: int | None = None
    stop_on_human_gate: bool = True
    stop_on_infrastructure_error: bool = True
    hard_grace_seconds: int = 300

    def __post_init__(self) -> None:
        if self.max_phases < 1 or self.max_wall_time_seconds < 1:
            raise CwError("Execution budget must be positive", ErrorCode.USAGE_ERROR, exit_code=2)
        if self.max_semantic_revisions_per_phase < 1:
            raise CwError("Semantic revision budget must be positive", ErrorCode.USAGE_ERROR, exit_code=2)
        if self.max_agent_runs is not None and self.max_agent_runs < 1:
            raise CwError("Agent run budget must be positive", ErrorCode.USAGE_ERROR, exit_code=2)
