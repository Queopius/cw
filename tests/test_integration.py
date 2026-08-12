from __future__ import annotations

import unittest

from cw.agents.reviewer import run_review
from cw.core.gates import validate_gate
from cw.core.models import WorkflowState
from cw.core.state import transition
from tests.helpers import FakeAdapter, TempRepo, result


class WorkflowIntegrationTest(unittest.TestCase):
    def test_approve_revise_approve_complete(self):
        repo = TempRepo(phases=2)
        try:
            repo.artifact(1); repo.ready(1)
            run_review(repo.root, repo.workflow, repo.workflow.phases[0], repo.state(), FakeAdapter(result(1)))
            validate_gate(repo.root, repo.workflow, "01-phase-1")
            state = repo.state(); state["current_phase"] = "02-phase-2"; state["attempt"] = 0
            transition(repo.root, state, WorkflowState.IN_PROGRESS)
            repo.artifact(2); repo.ready(2)
            revised = run_review(repo.root, repo.workflow, repo.workflow.phases[1], repo.state(), FakeAdapter(result(2, "REVISE", "FAIL")))
            self.assertEqual("REVISE", revised["decision"])
            self.assertEqual("02-phase-2", repo.state()["current_phase"])
            repo.ready(2)
            approved = run_review(repo.root, repo.workflow, repo.workflow.phases[1], repo.state(), FakeAdapter(result(2)))
            self.assertEqual("APPROVE", approved["decision"])
            state = repo.state(); transition(repo.root, state, WorkflowState.COMPLETED)
            self.assertEqual("COMPLETED", repo.state()["status"])
            self.assertTrue((repo.root / ".cw/gates/01-phase-1.approved.json").exists())
            self.assertTrue((repo.root / ".cw/gates/02-phase-2.approved.json").exists())
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
