from __future__ import annotations

import unittest

from cw.agents.reviewer import run_review
from cw.core.config import CORE_PROTECTED_PATHS
from cw.core.errors import CwError, ErrorCode
from cw.core.integrity import snapshot_protected_paths, verify_protected_paths
from tests.helpers import FakeAdapter, TempRepo, result


class ProtectedPathTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.phase = self.repo.workflow.phases[0]
        self.paths = CORE_PROTECTED_PATHS

    def tearDown(self):
        self.repo.close()

    def test_unchanged_snapshot_passes(self):
        before = snapshot_protected_paths(self.repo.root, self.paths)
        verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)

    def test_existing_protected_review_cannot_be_modified(self):
        self.repo.artifact()
        reference = self.repo.approved_review()
        before = snapshot_protected_paths(self.repo.root, self.paths)
        (self.repo.root / reference).write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as raised:
            verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)
        self.assertEqual(ErrorCode.PROTECTED_PATH_MODIFIED, raised.exception.code)

    def test_workflow_plan_cannot_be_modified(self):
        before = snapshot_protected_paths(self.repo.root, self.paths)
        plan = self.repo.root / ".codex/workflow/phases.yaml"
        plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(CwError) as raised:
            verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)
        self.assertEqual(ErrorCode.PROTECTED_PATH_MODIFIED, raised.exception.code)

    def test_unrecorded_review_is_rejected(self):
        before = snapshot_protected_paths(self.repo.root, self.paths)
        (self.repo.root / ".cw/reviews/forged.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as raised:
            verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)
        self.assertEqual(ErrorCode.PROTECTED_PATH_MODIFIED, raised.exception.code)

    def test_state_cannot_advance_without_review(self):
        before = snapshot_protected_paths(self.repo.root, self.paths)
        state = self.repo.state()
        state["status"] = "APPROVED"
        from cw.core.state import save_state
        save_state(self.repo.root, state)
        with self.assertRaises(CwError) as raised:
            verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)
        self.assertEqual(ErrorCode.PROTECTED_PATH_MODIFIED, raised.exception.code)

    def test_legitimate_hook_review_and_gate_are_allowed(self):
        self.repo.artifact()
        self.repo.ready()
        before = snapshot_protected_paths(self.repo.root, self.paths)
        run_review(self.repo.root, self.repo.workflow, self.phase, self.repo.state(), FakeAdapter(result()))
        verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)

    def test_legitimate_revision_is_allowed(self):
        self.repo.artifact()
        self.repo.ready()
        before = snapshot_protected_paths(self.repo.root, self.paths)
        run_review(
            self.repo.root, self.repo.workflow, self.phase, self.repo.state(),
            FakeAdapter(result(decision="REVISE", status="FAIL")),
        )
        verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)

    def test_legitimate_review_infrastructure_error_is_allowed(self):
        self.repo.artifact()
        self.repo.ready()
        before = snapshot_protected_paths(self.repo.root, self.paths)
        failure = CwError("network", ErrorCode.REVIEWER_NETWORK_ERROR)
        with self.assertRaises(CwError):
            run_review(
                self.repo.root, self.repo.workflow, self.phase, self.repo.state(),
                FakeAdapter(error=failure),
            )
        verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)

    def test_gate_without_new_review_is_rejected(self):
        self.repo.artifact()
        reference = self.repo.approved_review()
        before = snapshot_protected_paths(self.repo.root, self.paths)
        from cw.core.gates import create_gate
        create_gate(self.repo.root, self.repo.workflow, self.phase, reference)
        state = self.repo.state()
        state["last_gate"] = ".cw/gates/01-phase-1.approved.json"
        from cw.core.state import save_state
        save_state(self.repo.root, state)
        with self.assertRaises(CwError):
            verify_protected_paths(self.repo.root, self.repo.workflow, self.phase, before)

    def test_glob_policy_path_is_rejected(self):
        with self.assertRaises(CwError) as raised:
            snapshot_protected_paths(self.repo.root, (".cw/reviews/*",))
        self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
