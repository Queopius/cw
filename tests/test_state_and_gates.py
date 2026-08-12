from __future__ import annotations

import unittest

from cw.agents.reviewer import run_review
from cw.core.errors import CwError
from cw.core.gates import create_gate, validate_gate
from cw.core.models import WorkflowState
from cw.core.state import transition
from tests.helpers import FakeAdapter, TempRepo, result


class StateAndGateTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()

    def tearDown(self):
        self.repo.close()

    def test_valid_transition(self):
        state = self.repo.state()
        transition(self.repo.root, state, WorkflowState.READY_FOR_REVIEW)
        self.assertEqual("READY_FOR_REVIEW", self.repo.state()["status"])

    def test_invalid_transition(self):
        with self.assertRaises(CwError):
            transition(self.repo.root, self.repo.state(), WorkflowState.APPROVED)

    def test_phase_advancement_requires_gate(self):
        with self.assertRaises(CwError):
            validate_gate(self.repo.root, self.repo.workflow, "01-phase-1")

    def test_gate_contains_valid_sha256(self):
        artifact = self.repo.artifact()
        gate = create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], ".cw/reviews/review.json")
        self.assertIn("sha256:", gate.read_text())
        self.assertEqual(64, len(__import__("json").loads(gate.read_text())["artifact_hashes"]["docs/phase-1.md"].split(":")[1]))

    def test_modified_artifact_invalidates_gate(self):
        artifact = self.repo.artifact()
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], "review")
        artifact.write_text("changed", encoding="utf-8")
        with self.assertRaises(CwError):
            validate_gate(self.repo.root, self.repo.workflow, "01-phase-1")

    def test_missing_dependency_gate_blocks(self):
        self.repo.artifact(2)
        from cw.core.gates import validate_dependencies
        with self.assertRaises(CwError):
            validate_dependencies(self.repo.root, self.repo.workflow, self.repo.workflow.phases[1])

    def test_approve_creates_gate(self):
        self.repo.artifact(); self.repo.ready()
        report = run_review(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], self.repo.state(), FakeAdapter(result()))
        self.assertEqual("APPROVE", report["decision"])
        self.assertTrue((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())


if __name__ == "__main__":
    unittest.main()
