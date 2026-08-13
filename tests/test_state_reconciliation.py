from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cw.agents.reviewer import run_review
from cw.cli.main import main
from cw.core.errors import CwError
from cw.core.config import CORE_PROTECTED_PATHS
from cw.core.integrity import (
    phase_contract_fingerprint,
    snapshot_protected_paths,
    verify_protected_paths,
)
from cw.core.initialize import repair
from cw.core.progress import derive_workflow_consistency
from cw.core.state import load_state, save_state, validate_state
from cw.core.workflow import load_workflow, set_plan_status
from tests.helpers import FakeAdapter, TempRepo, result


class WorkflowStateReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=3)
        self.previous = Path.cwd()
        os.chdir(self.repo.root)

    def tearDown(self) -> None:
        os.chdir(self.previous)
        self.repo.close()

    def review(self, phase: int, decision: str = "APPROVE") -> None:
        self.repo.artifact(phase)
        self.repo.ready(phase)
        run_review(
            self.repo.root,
            self.repo.workflow,
            self.repo.workflow.phases[phase - 1],
            self.repo.state(),
            FakeAdapter(result(phase, decision, "FAIL" if decision == "REVISE" else "PASS")),
        )

    def stale_through_second_gate(self) -> tuple[bytes, bytes]:
        self.review(1)
        self.review(2, "REVISE")
        self.repo.ready(2)
        run_review(
            self.repo.root,
            self.repo.workflow,
            self.repo.workflow.phases[1],
            self.repo.state(),
            FakeAdapter(result(2)),
        )
        gate_one = self.repo.root / ".cw/gates/01-phase-1.approved.json"
        gate_two = self.repo.root / ".cw/gates/02-phase-2.approved.json"
        state = self.repo.state()
        state["history"] = [
            event for event in state["history"]
            if not (event.get("phase") == "02-phase-2" and event.get("action") == "approved")
        ]
        state.update({
            "current_phase": "01-phase-1",
            "status": "ERROR",
            "attempt": 1,
            "last_gate": ".cw/gates/01-phase-1.approved.json",
            "last_review": next(
                path.relative_to(self.repo.root).as_posix()
                for path in sorted((self.repo.root / ".cw/reviews").glob("02-phase-2-attempt-01*.json"))
            ),
            "last_error": "PROTECTED_PATH_MODIFIED: stale project metadata baseline",
            "infrastructure_error": None,
        })
        save_state(self.repo.root, state)
        return gate_one.read_bytes(), gate_two.read_bytes()

    def invoke(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, output.getvalue()

    def test_status_detects_contradiction_without_mutating_then_repair_advances(self) -> None:
        gate_one, gate_two = self.stale_through_second_gate()
        before = (self.repo.root / ".cw/state.json").read_bytes()

        code, output = self.invoke("status", "--no-color")

        self.assertEqual(1, code)
        self.assertIn("STATE INCONSISTENT", output)
        self.assertIn("Expected phase", output)
        self.assertIn("03-phase-3", output)
        self.assertEqual(before, (self.repo.root / ".cw/state.json").read_bytes())

        report: dict = {}
        backup = repair(self.repo.root, report=report)
        state = load_state(self.repo.root)
        self.assertEqual("03-phase-3", state["current_phase"])
        self.assertEqual("IN_PROGRESS", state["status"])
        self.assertEqual(0, state["attempt"])
        self.assertEqual(".cw/gates/02-phase-2.approved.json", state["last_gate"])
        self.assertIn("02-phase-2", state["last_review"])
        self.assertIsNone(state["last_error"])
        self.assertIsNone(state["infrastructure_error"])
        self.assertEqual(gate_one, (self.repo.root / ".cw/gates/01-phase-1.approved.json").read_bytes())
        self.assertEqual(gate_two, (self.repo.root / ".cw/gates/02-phase-2.approved.json").read_bytes())
        self.assertIn("stale project metadata baseline", (backup / "state.json").read_text())
        approvals = [event for event in state["history"] if event.get("phase") == "02-phase-2"]
        self.assertEqual(["revision_required", "approved"], [event["action"] for event in approvals])
        self.assertEqual(1, report["history_reconstructed"])
        validate_state(self.repo.root, state, load_workflow(self.repo.root))

    def test_repair_is_idempotent_and_refreshes_writer_versions(self) -> None:
        self.stale_through_second_gate()
        project_path = self.repo.root / ".cw/project.json"
        state_path = self.repo.root / ".cw/state.json"
        project = json.loads(project_path.read_text())
        state = json.loads(state_path.read_text())
        project.pop("created_with_cw_version", None)
        state.pop("created_with_cw_version", None)
        project["cw_version"] = "0.3.0"
        state["cw_version"] = "0.1.4"
        project_path.write_text(json.dumps(project), encoding="utf-8")
        state_path.write_text(json.dumps(state), encoding="utf-8")

        repair(self.repo.root)
        repaired_project = json.loads(project_path.read_text())
        repaired_state = json.loads(state_path.read_text())
        self.assertEqual("0.3.0", repaired_project["created_with_cw_version"])
        self.assertEqual("0.1.4", repaired_state["created_with_cw_version"])
        self.assertEqual(repaired_project["cw_version"], repaired_state["cw_version"])
        first_project = project_path.read_bytes()
        first_state = state_path.read_bytes()
        repair(self.repo.root)
        self.assertEqual(first_project, project_path.read_bytes())
        self.assertEqual(first_state, state_path.read_bytes())

    def test_broken_chain_does_not_count_later_gate(self) -> None:
        self.review(1)
        self.review(2)
        (self.repo.root / ".cw/gates/01-phase-1.approved.json").unlink()
        consistency = derive_workflow_consistency(
            self.repo.root, self.repo.workflow, self.repo.state(),
        )
        self.assertEqual(0, len(consistency.chain.approved))
        self.assertEqual("invalid", consistency.chain.states["02-phase-2"])
        with self.assertRaises(CwError):
            repair(self.repo.root)

    def test_old_readiness_and_stale_attempt_are_reconciled(self) -> None:
        self.review(1)
        state = self.repo.state()
        state["attempt"] = 2
        save_state(self.repo.root, state)
        readiness = self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        readiness.write_text(json.dumps({"schema_version": 1, "phase": "01-phase-1"}), encoding="utf-8")
        self.assertFalse(derive_workflow_consistency(self.repo.root, self.repo.workflow, state).consistent)
        repair(self.repo.root)
        self.assertFalse(readiness.exists())
        self.assertEqual(0, load_state(self.repo.root)["attempt"])

    def test_plan_with_gates_cannot_remain_proposed(self) -> None:
        self.review(1)
        set_plan_status(self.repo.root, "PROPOSED")
        workflow = load_workflow(self.repo.root)
        self.assertFalse(derive_workflow_consistency(self.repo.root, workflow, self.repo.state()).consistent)
        repair(self.repo.root)
        self.assertEqual("APPROVED", load_workflow(self.repo.root).status)

    def test_resolved_cw_metadata_error_is_archived_and_cleared(self) -> None:
        self.review(1)
        state = self.repo.state()
        state.update({
            "status": "ERROR",
            "last_error": (
                "PROTECTED_PATH_MODIFIED: Protected workflow state changed without a review"
            ),
            "infrastructure_error": None,
        })
        save_state(self.repo.root, state)
        self.assertFalse(
            derive_workflow_consistency(self.repo.root, self.repo.workflow, state).consistent
        )

        backup = repair(self.repo.root)

        repaired = load_state(self.repo.root)
        self.assertEqual("IN_PROGRESS", repaired["status"])
        self.assertEqual("02-phase-2", repaired["current_phase"])
        self.assertIsNone(repaired["last_error"])
        self.assertIn("Protected workflow state changed", (backup / "state.json").read_text())

    def test_missing_revision_and_approval_events_are_reconstructed_from_evidence(self) -> None:
        self.stale_through_second_gate()
        state = self.repo.state()
        state["history"] = [
            event for event in state["history"]
            if event.get("phase") != "02-phase-2"
        ]
        save_state(self.repo.root, state)
        source_review = json.loads(next(
            path for path in sorted((self.repo.root / ".cw/reviews").glob("02-phase-2-attempt-01*.json"))
        ).read_text())
        report: dict = {}

        repair(self.repo.root, report=report)

        events = [
            event for event in load_state(self.repo.root)["history"]
            if event.get("phase") == "02-phase-2"
        ]
        self.assertEqual(["revision_required", "approved"], [event["action"] for event in events])
        self.assertEqual([1, 2], [event["attempt"] for event in events])
        self.assertEqual(source_review["created_at"], events[0]["timestamp"])
        self.assertEqual(2, report["history_reconstructed"])

    def test_project_metadata_is_separate_from_phase_contract_but_still_protected(self) -> None:
        phase = self.repo.workflow.phases[0]
        contract = phase_contract_fingerprint(self.repo.workflow, phase)
        before = snapshot_protected_paths(
            self.repo.root,
            CORE_PROTECTED_PATHS,
            workflow=self.repo.workflow,
            phase=phase,
        )
        project_path = self.repo.root / ".cw/project.json"
        project = json.loads(project_path.read_text())
        project["cw_version"] = "99.0.0"
        project_path.write_text(json.dumps(project), encoding="utf-8")
        self.assertEqual(contract, phase_contract_fingerprint(self.repo.workflow, phase))
        with self.assertRaises(CwError):
            verify_protected_paths(self.repo.root, self.repo.workflow, phase, before)

    def test_timeline_cannot_render_approved_phase_as_current(self) -> None:
        self.stale_through_second_gate()
        _, output = self.invoke("status", "--no-color")
        self.assertNotIn("✓ 01", output)
        self.assertNotIn("→ 01", output)
        self.assertNotIn("✓ 02", output)


if __name__ == "__main__":
    unittest.main()
