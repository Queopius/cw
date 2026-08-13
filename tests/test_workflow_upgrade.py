from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cw.agents.reviewer import run_review
from cw.cli.commands.read import render_status, status_payload
from cw.cli.main import _context, main
from cw.core.gates import validate_gate
from cw.core.history import history_timeline
from cw.core.initialize import repair
from cw.core.models import WorkflowState
from cw.core.state import load_state, save_state
from cw.core.workflow import load_workflow, set_plan_status
from cw.ui.console import Console
from tests.helpers import FakeAdapter, TempRepo, result


class PostApprovalSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=2)

    def tearDown(self) -> None:
        self.repo.close()

    def approve(self, phase: int = 1) -> dict:
        self.repo.artifact(phase)
        self.repo.ready(phase)
        return run_review(
            self.repo.root,
            self.repo.workflow,
            self.repo.workflow.phases[phase - 1],
            self.repo.state(),
            FakeAdapter(result(phase)),
        )

    def test_non_final_approval_advances_and_resets_runtime(self) -> None:
        report = self.approve(1)
        state = self.repo.state()
        self.assertEqual("02-phase-2", state["current_phase"])
        self.assertEqual(WorkflowState.IN_PROGRESS.value, state["status"])
        self.assertEqual(0, state["attempt"])
        self.assertFalse((self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json").exists())
        self.assertFalse((self.repo.root / ".cw/gates/02-phase-2.approved.json").exists())
        self.assertTrue((self.repo.root / ".cw/gates/01-phase-1.approved.json").is_file())
        self.assertEqual("02-phase-2", report["next_phase"])
        validate_gate(self.repo.root, self.repo.workflow, "01-phase-1")

    def test_final_approval_completes_without_inventing_phase(self) -> None:
        self.approve(1)
        report = self.approve(2)
        state = self.repo.state()
        self.assertEqual(WorkflowState.COMPLETED.value, state["status"])
        self.assertEqual("02-phase-2", state["current_phase"])
        self.assertTrue(report["workflow_completed"])
        self.assertIsNone(report["next_phase"])

    def test_revision_does_not_advance(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1, "REVISE", "FAIL")),
        )
        state = self.repo.state()
        self.assertEqual("01-phase-1", state["current_phase"])
        self.assertEqual(WorkflowState.REVISION_REQUIRED.value, state["status"])
        self.assertEqual(1, state["attempt"])

    def test_repair_promotes_evidenced_legacy_plan_and_advances_stale_state(self) -> None:
        self.approve(1)
        set_plan_status(self.repo.root, "PROPOSED")
        state = self.repo.state()
        state.update({"current_phase": "01-phase-1", "status": "APPROVED", "attempt": 1})
        save_state(self.repo.root, state)

        backup = repair(self.repo.root)

        workflow = load_workflow(self.repo.root)
        repaired = load_state(self.repo.root)
        self.assertEqual("APPROVED", workflow.status)
        self.assertEqual("02-phase-2", repaired["current_phase"])
        self.assertEqual("IN_PROGRESS", repaired["status"])
        self.assertEqual(0, repaired["attempt"])
        self.assertTrue((backup / "phases.yaml").is_file())
        validate_gate(self.repo.root, workflow, "01-phase-1")

        plan_after_first = (self.repo.root / ".codex/workflow/phases.yaml").read_bytes()
        state_after_first = (self.repo.root / ".cw/state.json").read_bytes()
        repair(self.repo.root)
        self.assertEqual(plan_after_first, (self.repo.root / ".codex/workflow/phases.yaml").read_bytes())
        self.assertEqual(state_after_first, (self.repo.root / ".cw/state.json").read_bytes())

    def test_repair_does_not_approve_unevidenced_plan(self) -> None:
        set_plan_status(self.repo.root, "PROPOSED")
        state = self.repo.state()
        state.update({"status": "PLAN_PROPOSED", "current_phase": "01-phase-1"})
        save_state(self.repo.root, state)
        repair(self.repo.root)
        self.assertEqual("PROPOSED", load_workflow(self.repo.root).status)


class HistoryReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=2)

    def tearDown(self) -> None:
        self.repo.close()

    def test_gate_only_history_appears_once_with_current_phase(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1)),
        )
        linked = Path(self.repo.state()["last_review"])
        duplicate = self.repo.root / ".cw/reviews/01-phase-1-duplicate.json"
        duplicate.write_bytes((self.repo.root / linked).read_bytes())

        timeline = history_timeline(self.repo.root, self.repo.workflow, self.repo.state())

        first = timeline[0]
        self.assertEqual(1, sum(entry["kind"] == "approved" for entry in first["entries"]))
        self.assertEqual("approved", first["entries"][-1]["kind"])
        self.assertTrue(timeline[1]["current"])

    def test_revision_then_approval_is_reconstructed(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1, "REVISE", "FAIL")),
        )
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1)),
        )
        entries = history_timeline(self.repo.root, self.repo.workflow, self.repo.state())[0]["entries"]
        self.assertEqual(["revision_required", "approved"], [entry["kind"] for entry in entries])
        self.assertEqual([1, 2], [entry["attempt"] for entry in entries])

    def test_current_entry_has_no_fabricated_timestamp(self) -> None:
        timeline = history_timeline(self.repo.root, self.repo.workflow, self.repo.state())
        self.assertIsNone(timeline[0]["entries"][0]["timestamp"])


class ProfessionalCliRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=2)
        self.previous = Path.cwd()
        os.chdir(self.repo.root)

    def tearDown(self) -> None:
        os.chdir(self.previous)
        self.repo.close()

    def invoke(self, *args: str) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args)
        return code, stream.getvalue()

    def test_status_distinguishes_position_and_approval_count(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1)),
        )
        code, output = self.invoke("status")
        self.assertEqual(0, code)
        self.assertIn("Position", output)
        self.assertIn("2 / 2", output)
        self.assertIn("Approved", output)
        self.assertIn("1 / 2", output)
        self.assertIn("✓ 01", output)
        self.assertIn("→ 02", output)

    def test_invalid_gate_uses_attention_marker(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1)),
        )
        self.repo.artifact(1, "tampered\n")
        code, output = self.invoke("status")
        self.assertEqual(1, code)
        self.assertIn("! 01", output)
        self.assertIn("Approval gate invalidated", output)

    def test_json_status_is_structured_and_ansi_free(self) -> None:
        code, output = self.invoke("status", "--json")
        payload = json.loads(output)
        self.assertEqual(0, code)
        self.assertEqual(1, payload["position"])
        self.assertEqual(0, payload["approved_count"])
        self.assertNotIn("\x1b[", output)

    def test_verbose_status_contains_path_while_normal_status_does_not(self) -> None:
        _, normal = self.invoke("status")
        _, verbose = self.invoke("status", "--verbose")
        self.assertNotIn(str(self.repo.root), normal)
        self.assertIn(str(self.repo.root), verbose)

    def test_long_phase_name_wraps_without_losing_marker(self) -> None:
        data = status_payload(self.repo.root, _context)
        data["phases"][0]["name"] = "A deliberately long phase name " * 8
        stream = io.StringIO()
        render_status(Console(stream=stream), data)
        output = stream.getvalue()
        self.assertIn("→ 01", output)
        self.assertGreater(len(output.splitlines()), 10)


if __name__ == "__main__":
    unittest.main()
