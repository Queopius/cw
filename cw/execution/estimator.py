from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any


@dataclass(frozen=True, slots=True)
class Estimate:
    minimum_seconds: int | None
    maximum_seconds: int | None
    confidence: str
    basis: str


class ExecutionEstimator:
    def estimate(
        self,
        history: list[dict[str, Any]],
        phases: int,
        *,
        completed_durations: list[int] | None = None,
    ) -> Estimate:
        starts: dict[str, datetime] = {}
        durations: list[float] = []
        for event in history:
            phase = event.get("phase")
            timestamp = _timestamp(event.get("timestamp"))
            if not isinstance(phase, str) or timestamp is None:
                continue
            if event.get("action") in {"phase_started", "batch_phase_started"}:
                starts[phase] = timestamp
            elif event.get("action") in {"approved", "human_approved"} and phase in starts:
                seconds = (timestamp - starts[phase]).total_seconds()
                if 0 < seconds < 24 * 3600:
                    durations.append(seconds)
        durations.extend(float(value) for value in (completed_durations or []) if 0 < value < 24 * 3600)
        if not durations:
            return Estimate(None, None, "insufficient history", "unavailable")
        center = median(durations) * phases
        return Estimate(max(60, int(center * 0.75)), int(center * 1.5), "medium" if len(durations) >= 3 else "low", "project-history")


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
