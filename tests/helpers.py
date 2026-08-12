from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from cw.adapters.codex import CodexResult
from cw.core.initialize import initialize
from cw.core.gates import artifact_hashes
from cw.core.models import WorkflowState
from cw.core.state import bind_plan, load_state, transition
from cw.core.workflow import load_workflow, set_plan_status, write_workflow, workflow_hash


class TempRepo:
    def __init__(self, name: str = "sample-app", phases: int = 2) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-test-")
        self.root = Path(self.temporary.name) / name
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.project, _ = initialize(self.root)
        plan_phases = []
        previous = None
        for index in range(1, phases + 1):
            phase_id = f"{index:02d}-phase-{index}"
            plan_phases.append({
                "id": phase_id, "name": f"Phase {index}", "objective": f"Deliver phase {index}",
                "depends_on": [previous] if previous else [], "artifacts": [f"docs/phase-{index}.md"],
                "review_paths": ["docs/**/*"], "required_commands": [],
                "acceptance_criteria": [{"id": f"P{index}-001", "description": f"Phase {index} is complete", "severity": "blocking"}],
                "blocking_criteria": [], "requires_human_approval": False,
            })
            previous = phase_id
        payload = {
            "schema_version": 1,
            "workflow": {"id": name, "repository": name, "version": 1, "status": "PROPOSED", "goal": "Test goal"},
            "settings": {"max_review_attempts": 3, "command_timeout_seconds": 30},
            "reviewer": {"command": "codex", "timeout_seconds": 30, "sandbox": "read-only"},
            "phases": plan_phases,
        }
        write_workflow(self.root / ".codex" / "workflow" / "phases.yaml", payload)
        self.workflow = load_workflow(self.root)
        state = load_state(self.root)
        transition(self.root, state, WorkflowState.PLANNING)
        bind_plan(self.root, state, self.workflow)
        transition(self.root, state, WorkflowState.PLAN_PROPOSED)
        set_plan_status(self.root, "APPROVED")
        self.workflow = load_workflow(self.root)
        state["workflow_sha256"] = workflow_hash(self.root / ".codex" / "workflow" / "phases.yaml")
        transition(self.root, state, WorkflowState.READY)
        transition(self.root, state, WorkflowState.IN_PROGRESS)

    def close(self) -> None:
        self.temporary.cleanup()

    def state(self):
        return load_state(self.root)

    def artifact(self, phase: int = 1, content: str = "complete\n") -> Path:
        path = self.root / "docs" / f"phase-{phase}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def ready(self, phase: int = 1, artifacts: list[str] | None = None, checks=None, session_id: str | None = None) -> None:
        path = self.root / ".cw" / "runtime" / "READY_FOR_REVIEW.json"
        session = self.root / ".cw" / "runtime" / "implementer-session.json"
        if session_id is None and session.is_file():
            session_id = json.loads(session.read_text(encoding="utf-8"))["session_id"]
        payload = {
            "phase": f"{phase:02d}-phase-{phase}", "status": "READY_FOR_REVIEW",
            "artifacts": artifacts or [f"docs/phase-{phase}.md"], "checks_executed": checks or [],
        }
        if session_id is not None:
            payload["session_id"] = session_id
        path.write_text(json.dumps(payload), encoding="utf-8")

    def approved_review(self, phase: int = 1, *, decision: str = "APPROVE") -> str:
        phase_model = self.workflow.phases[phase - 1]
        path = self.root / ".cw" / "reviews" / f"{phase_model.id}-fixture.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "workflow": self.workflow.id,
            "phase": phase_model.id,
            "attempt": 1,
            "kind": "semantic_review",
            "decision": decision,
            "criteria": [{
                "id": phase_model.acceptance_criteria[0].id,
                "status": "PASS",
                "evidence": ["fixture evidence"],
            }],
            "blocking_issues": [],
            "artifact_hashes": artifact_hashes(self.root, phase_model.artifacts),
            "created_at": "2026-08-12T00:00:00Z",
        }, indent=2), encoding="utf-8")
        return path.relative_to(self.root).as_posix()


class FakeAdapter:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def run_reviewer(self, root, prompt, schema, timeout):
        if self.error:
            raise self.error
        return CodexResult(self.payload, "")


def result(phase: int = 1, decision: str = "APPROVE", status: str = "PASS", *, criterion: str | None = None):
    return {
        "decision": decision, "summary": "reviewed", "blocking_issues": [] if status == "PASS" else ["needs work"],
        "criteria": [{"id": criterion or f"P{phase}-001", "status": status, "evidence": ["docs evidence"]}],
    }
