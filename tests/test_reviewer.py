from __future__ import annotations

import unittest
from dataclasses import replace

from cw.agents.reviewer import human_approve, run_review
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import validate_gate
from tests.helpers import FakeAdapter, TempRepo, result


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.repo.artifact(); self.repo.ready()

    def tearDown(self):
        self.repo.close()

    def review(self, payload):
        return run_review(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], self.repo.state(), FakeAdapter(payload))

    def test_approve(self):
        self.assertEqual("APPROVE", self.review(result())["decision"])

    def test_revise(self):
        self.assertEqual("REVISE", self.review(result(decision="REVISE", status="FAIL"))["decision"])
        self.assertEqual(1, self.repo.state()["attempt"])

    def test_human_review_required(self):
        payload = result(decision="HUMAN_REVIEW_REQUIRED")
        self.assertEqual("HUMAN_REVIEW_REQUIRED", self.review(payload)["decision"])

    def test_invalid_schema(self):
        with self.assertRaises(CwError):
            self.review({"decision": "APPROVE"})
        self.assertEqual(0, self.repo.state()["attempt"])
        self.assertEqual("ERROR", self.repo.state()["status"])

    def test_missing_criterion_fails_closed(self):
        payload = result(); payload["criteria"] = []
        self.assertEqual("REVISE", self.review(payload)["decision"])

    def test_invented_criterion_fails_closed(self):
        self.assertEqual("REVISE", self.review(result(criterion="INVENTED"))["decision"])

    def test_unknown_evidence_fails_closed(self):
        self.assertEqual("REVISE", self.review(result(status="UNKNOWN"))["decision"])

    def test_infrastructure_timeout_does_not_consume_attempt(self):
        error = CwError("timed out", ErrorCode.REVIEW_TIMEOUT)
        with self.assertRaises(CwError):
            run_review(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], self.repo.state(), FakeAdapter(error=error))
        self.assertEqual(0, self.repo.state()["attempt"])

    def test_network_error_does_not_consume_attempt(self):
        error = CwError("network", ErrorCode.REVIEWER_NETWORK_ERROR)
        with self.assertRaises(CwError):
            run_review(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], self.repo.state(), FakeAdapter(error=error))
        self.assertEqual(0, self.repo.state()["attempt"])

    def test_infrastructure_report_redacts_credentials(self):
        error = CwError(
            "network", ErrorCode.REVIEWER_NETWORK_ERROR,
            details="Authorization: Bearer reviewer-secret-token",
        )
        with self.assertRaises(CwError):
            self.review_adapter_error(error)
        reports = list((self.repo.root / ".cw/reviews").glob("*-infrastructure-*.json"))
        self.assertEqual(1, len(reports))
        self.assertNotIn("reviewer-secret-token", reports[0].read_text(encoding="utf-8"))

    def review_adapter_error(self, error):
        return run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(error=error),
        )

    def test_human_approval_rejects_post_review_artifact_change(self):
        phase = replace(self.repo.workflow.phases[0], requires_human_approval=True)
        workflow = replace(self.repo.workflow, phases=(phase, *self.repo.workflow.phases[1:]))
        run_review(self.repo.root, workflow, phase, self.repo.state(), FakeAdapter(result()))
        self.repo.artifact(content="changed after semantic review")
        with self.assertRaises(CwError):
            human_approve(self.repo.root, workflow, phase, self.repo.state())

    def test_human_approval_gate_records_and_validates_approval_type(self):
        phase = replace(self.repo.workflow.phases[0], requires_human_approval=True)
        workflow = replace(self.repo.workflow, phases=(phase, *self.repo.workflow.phases[1:]))
        run_review(self.repo.root, workflow, phase, self.repo.state(), FakeAdapter(result()))
        gate = human_approve(self.repo.root, workflow, phase, self.repo.state())
        import json
        self.assertEqual("human", json.loads(gate.read_text(encoding="utf-8"))["approval"]["kind"])
        validate_gate(self.repo.root, workflow, phase.id)


if __name__ == "__main__":
    unittest.main()
