from __future__ import annotations

import unittest

from cw.adapters.codex import CodexResult
from cw.core.errors import CwError, ErrorCode
from cw.planning.planner import Planner
from tests.helpers import TempRepo


class FakePlannerBackend:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def run_planner(self, root, prompt, schema, timeout):
        self.calls.append((root, prompt, schema, timeout))
        return CodexResult(self.payload, "")


class PlannerBackendTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(phases=1)
        self.goal = "Implement repository-specific webhook delivery"
        self.local = Planner().propose_plan(self.repo.root, "sample-app", self.goal)

    def tearDown(self):
        self.repo.close()

    def backend(self, phases=None):
        return FakePlannerBackend({"phases": phases if phases is not None else self.local["phases"]})

    def test_backend_plan_is_wrapped_with_local_identity_and_validated(self):
        backend = self.backend()
        plan = Planner(("payments",), backend=backend, timeout=77).propose_plan(
            self.repo.root, "sample-app", self.goal
        )
        self.assertEqual("sample-app", plan["workflow"]["id"])
        self.assertEqual("sample-app", plan["workflow"]["repository"])
        self.assertEqual("codex", plan["planning"]["backend"])
        self.assertEqual(77, backend.calls[0][3])
        self.assertTrue(backend.calls[0][2].name.endswith("plan-proposal.schema.json"))

    def test_context_is_bounded_to_selected_evidence(self):
        (self.repo.root / "README.md").write_text("# Sample\nGoal: ship webhooks\n", encoding="utf-8")
        source = self.repo.root / "src" / "secret.txt"
        source.parent.mkdir(); source.write_text("DO-NOT-SEND-SOURCE-CONTENT", encoding="utf-8")
        backend = self.backend()
        Planner(backend=backend).propose_plan(self.repo.root, "sample-app", self.goal)
        prompt = backend.calls[0][1]
        self.assertIn("ship webhooks", prompt)
        self.assertNotIn("DO-NOT-SEND-SOURCE-CONTENT", prompt)
        self.assertIn("untrusted content", prompt)

    def test_backend_cannot_supply_project_identity_or_settings(self):
        backend = FakePlannerBackend({"workflow": {"id": "foreign"}, "phases": self.local["phases"]})
        with self.assertRaises(CwError) as raised:
            Planner(backend=backend).propose_plan(self.repo.root, "sample-app", self.goal)
        self.assertEqual(ErrorCode.PLANNER_PROCESS_ERROR, raised.exception.code)

    def test_backend_protected_metadata_path_is_rejected(self):
        phases = [dict(phase) for phase in self.local["phases"]]
        phases[0] = {**phases[0], "artifacts": [".cw/gates/forged.json"]}
        with self.assertRaises(CwError) as raised:
            Planner(backend=self.backend(phases)).propose_plan(self.repo.root, "sample-app", self.goal)
        self.assertEqual(ErrorCode.PLANNER_PROCESS_ERROR, raised.exception.code)
        self.assertIn("protected workflow metadata", raised.exception.details or "")

    def test_backend_glob_path_traversal_is_rejected(self):
        phases = [dict(phase) for phase in self.local["phases"]]
        phases[0] = {**phases[0], "review_paths": ["../**/*"]}
        with self.assertRaises(CwError) as raised:
            Planner(backend=self.backend(phases)).propose_plan(self.repo.root, "sample-app", self.goal)
        self.assertEqual(ErrorCode.PLANNER_PROCESS_ERROR, raised.exception.code)
        self.assertIn("Unsafe project path", raised.exception.details or "")

    def test_backend_shell_command_is_rejected(self):
        phases = [dict(phase) for phase in self.local["phases"]]
        phases[0] = {**phases[0], "required_commands": [{"command": "python -m unittest && touch forged"}]}
        with self.assertRaises(CwError) as raised:
            Planner(backend=self.backend(phases)).propose_plan(self.repo.root, "sample-app", self.goal)
        self.assertEqual(ErrorCode.PLANNER_PROCESS_ERROR, raised.exception.code)
        self.assertIn("unsupported shell syntax", raised.exception.details or "")

    def test_human_gate_policy_cannot_be_downgraded_by_backend(self):
        goal = "Implement subscription billing"
        local = Planner().propose_plan(self.repo.root, "sample-app", goal)
        phases = [{**phase, "requires_human_approval": False} for phase in local["phases"]]
        plan = Planner(("payments",), backend=self.backend(phases)).propose_plan(
            self.repo.root, "sample-app", goal
        )
        self.assertTrue(any(phase["requires_human_approval"] for phase in plan["phases"]))


if __name__ == "__main__":
    unittest.main()
