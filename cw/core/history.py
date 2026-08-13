from __future__ import annotations

from pathlib import Path
from typing import Any

from .audit import audit_history
from .gates import gate_path, validate_gate
from .legacy_evidence import is_legacy_review
from .models import Workflow
from .utils import load_json


def _review_decision(review: dict[str, Any]) -> str | None:
    if is_legacy_review(review):
        result = review.get("reviewer_result")
        return result.get("decision") if isinstance(result, dict) else None
    decision = review.get("decision")
    return decision if isinstance(decision, str) else None


def _timestamp(value: dict[str, Any]) -> str | None:
    for key in ("approved_at", "created_at", "timestamp", "reviewed_at"):
        timestamp = value.get(key)
        if isinstance(timestamp, str) and timestamp and timestamp != "unknown":
            return timestamp
    return None


def history_timeline(root: Path, workflow: Workflow, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a phase audit view from validated append-only evidence."""
    audit_history(root, workflow, state)
    reviews_by_phase: dict[str, list[tuple[str, dict[str, Any]]]] = {
        phase.id: [] for phase in workflow.phases
    }
    for path in sorted((root / ".cw" / "reviews").glob("*.json")):
        review = load_json(path)
        if isinstance(review, dict) and review.get("phase") in reviews_by_phase:
            reviews_by_phase[str(review["phase"])].append((path.relative_to(root).as_posix(), review))

    raw_events = state.get("history", []) if isinstance(state.get("history"), list) else []
    timeline: list[dict[str, Any]] = []
    for phase in workflow.phases:
        entries: list[dict[str, Any]] = []
        gate = None
        gate_reference = None
        linked_review = None
        if gate_path(root, phase.id).is_file():
            gate = validate_gate(root, workflow, phase.id)
            gate_reference = gate_path(root, phase.id).relative_to(root).as_posix()
            linked_review = gate.get("review_reference") or gate.get("review_file")

        infrastructure_seen = False
        for reference, review in reviews_by_phase[phase.id]:
            kind = review.get("kind")
            if kind == "infrastructure_error" or (
                is_legacy_review(review)
                and review.get("reviewer_result") is None
                and review.get("system_error") not in (None, "", {})
            ):
                if not infrastructure_seen:
                    entries.append({
                        "kind": "infrastructure_failure_recovered" if gate else "infrastructure_failure",
                        "attempt": None,
                        "timestamp": _timestamp(review),
                        "review": reference,
                        "error_code": review.get("error_code"),
                    })
                    infrastructure_seen = True
                continue
            decision = _review_decision(review)
            if decision == "REVISE":
                entries.append({
                    "kind": "revision_required",
                    "attempt": review.get("attempt"),
                    "timestamp": _timestamp(review),
                    "review": reference,
                })
            elif decision == "HUMAN_REVIEW_REQUIRED" and gate is None:
                entries.append({
                    "kind": "human_review_required",
                    "attempt": review.get("attempt"),
                    "timestamp": _timestamp(review),
                    "review": reference,
                })

        if not infrastructure_seen:
            recovered = next((
                event for event in raw_events
                if isinstance(event, dict)
                and event.get("phase") == phase.id
                and event.get("action") in {"infrastructure_error", "infrastructure_error_migrated"}
            ), None)
            if recovered is not None:
                entries.append({
                    "kind": "infrastructure_failure_recovered" if gate else "infrastructure_failure",
                    "attempt": None,
                    "timestamp": recovered.get("timestamp"),
                    "error_code": recovered.get("error_code"),
                })

        if gate is not None:
            linked = next((review for reference, review in reviews_by_phase[phase.id] if reference == linked_review), None)
            entries.append({
                "kind": "approved",
                "attempt": linked.get("attempt") if isinstance(linked, dict) else None,
                "timestamp": _timestamp(gate),
                "gate": gate_reference,
                "review": linked_review,
            })

        is_current = state.get("current_phase") == phase.id and state.get("status") != "COMPLETED"
        if is_current:
            entries.append({"kind": "current", "attempt": state.get("attempt", 0), "timestamp": None})
        if entries:
            timeline.append({
                "phase": phase.id,
                "number": phase.id.split("-", 1)[0],
                "name": phase.name,
                "approved": gate is not None,
                "current": is_current,
                "entries": entries,
            })
    return timeline
