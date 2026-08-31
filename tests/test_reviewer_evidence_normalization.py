from __future__ import annotations

import json
import unittest
from dataclasses import replace

from cw.core.audit import audit_history
from cw.core.revisions import audit_revisions, review_revision, supersession_index
from cw.core.reviews import normalize_evidence_references, validate_reviewer_result
from cw.core.state import load_state
from cw.core.utils import load_json
from cw.core.workflow import load_workflow
from tests.helpers import TempRepo
from tests import test_plan_revisions as plan_revision_tests


class ReviewerEvidenceNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        files = {
            "docs/phase-1.md": "phase\n",
            "src/service.py": "service\n",
            "tests/check.py": "check\n",
            "outside/other.py": "outside\n",
        }
        for relative, content in files.items():
            path = self.repo.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        base = self.repo.workflow.phases[0]
        self.phase = replace(
            base,
            artifacts=("docs/phase-1.md", "src/service.py", "tests/check.py"),
            review_paths=("docs/**/*", "src/**", "tests/**"),
            blocking_criteria=("Every reference is independently scoped",),
        )
        self.bundled = (
            "docs/phase-1.md", "src/service.py", "tests/check.py",
            "outside/other.py",
        )

    def tearDown(self) -> None:
        self.repo.close()

    def payload(self, evidence: list[str]) -> dict[str, object]:
        return {
            "decision": "APPROVE",
            "summary": "reviewed",
            "blocking_issues": [],
            "criteria": [{
                "id": "P1-001", "status": "PASS", "evidence": evidence,
            }],
            "blocking_criteria": [{
                "description": "Every reference is independently scoped",
                "status": "PASS",
                "evidence": ["docs/phase-1.md:1 blocking observation"],
            }],
        }

    def validate(self, evidence: list[str]):
        return validate_reviewer_result(
            self.phase,
            self.payload(evidence),
            require_blocking_criteria=True,
            strict=True,
            root=self.repo.root,
            evidence_paths=self.bundled,
        )

    def test_scalar_structured_grouped_and_multiline_forms_are_canonical(self) -> None:
        evidence = [
            "./docs\\phase-1.md:1 scalar observation",
            "src/service.py:1, tests/check.py:1-2, and ./src/service.py:1 prove grouping",
            "Citations:\n- tests/check.py:3 multiline observation\n- docs/phase-1.md:2 second line",
        ]

        decision, criteria, _blocking, issues = self.validate(evidence)

        self.assertEqual("APPROVE", decision.value)
        self.assertEqual([], issues)
        self.assertEqual([
            "docs/phase-1.md:1 scalar observation",
            "src/service.py:1 prove grouping",
            "tests/check.py:1-2 prove grouping",
            "tests/check.py:3 Citations: multiline observation",
            "docs/phase-1.md:2 second line",
        ], criteria[0]["evidence"])

    def test_stable_deduplication_and_round_trip_do_not_drift(self) -> None:
        first = [
            "./src/service.py:1 first observation",
            "src\\service.py:1 duplicate observation",
            "tests/check.py:2 second observation",
        ]
        _decision, criteria, blocking, issues = self.validate(first)
        self.assertEqual([], issues)

        round_trip = self.payload(criteria[0]["evidence"])
        round_trip["blocking_criteria"] = blocking
        round_trip = json.loads(json.dumps(round_trip))
        _decision2, criteria2, blocking2, issues2 = validate_reviewer_result(
            self.phase,
            round_trip,
            require_blocking_criteria=True,
            strict=True,
            root=self.repo.root,
            evidence_paths=self.bundled,
        )

        self.assertEqual([], issues2)
        self.assertEqual(criteria, criteria2)
        self.assertEqual(blocking, blocking2)

    def test_criterion_ids_and_explanatory_symbols_are_not_paths(self) -> None:
        normalized, recognized = normalize_evidence_references(
            ["01-ac-01 and request.digest are explanatory prose"],
            evidence_paths=frozenset(self.bundled),
        )
        self.assertEqual([], normalized)
        self.assertEqual([], recognized)

        decision, _criteria, _blocking, issues = self.validate([
            "docs/phase-1.md:1 01-ac-01 passes because request.digest and "
            "read/create behavior are complete",
        ])
        self.assertEqual("APPROVE", decision.value)
        self.assertEqual([], issues)

    def test_path_looking_human_explanation_is_not_a_second_reference(self) -> None:
        decision, criteria, _blocking, issues = self.validate([
            "docs/phase-1.md:1 explains why docs/not-evidence.py is only prose",
        ])

        self.assertEqual("APPROVE", decision.value)
        self.assertEqual([], issues)
        self.assertEqual([
            "docs/phase-1.md:1 explains why docs/not-evidence.py is only prose",
        ], criteria[0]["evidence"])

    def test_each_mixed_invalid_reference_fails_on_exact_reference(self) -> None:
        decision, criteria, _blocking, issues = self.validate([
            "docs/phase-1.md:1, ../escape.py:2, and outside/other.py:3 mixed evidence",
        ])

        self.assertEqual("REVISE", decision.value)
        self.assertEqual([
            "docs/phase-1.md:1 mixed evidence",
            "../escape.py:2 mixed evidence",
            "outside/other.py:3 mixed evidence",
        ], criteria[0]["evidence"])
        joined = "\n".join(issues)
        self.assertIn("../escape.py:2", joined)
        self.assertIn("outside/other.py:3", joined)

    def test_blocking_evidence_uses_the_same_independent_enforcement(self) -> None:
        payload = self.payload(["docs/phase-1.md:1 valid"])
        payload["blocking_criteria"][0]["evidence"] = [
            "src/service.py:1 and ../blocking-escape.py:9 mixed",
        ]

        decision, _criteria, blocking, issues = validate_reviewer_result(
            self.phase,
            payload,
            require_blocking_criteria=True,
            strict=True,
            root=self.repo.root,
            evidence_paths=self.bundled,
        )

        self.assertEqual("REVISE", decision.value)
        self.assertEqual([
            "src/service.py:1 mixed", "../blocking-escape.py:9 mixed",
        ], blocking[0]["evidence"])
        self.assertIn("../blocking-escape.py:9", "\n".join(issues))

    def test_malformed_range_and_prose_only_evidence_fail_closed(self) -> None:
        for evidence in (["src/service.py:9-2 invalid range"], ["01-ac-01 prose only"]):
            with self.subTest(evidence=evidence):
                decision, _criteria, _blocking, issues = self.validate(evidence)
                self.assertEqual("REVISE", decision.value)
                self.assertTrue(any("outside review scope" in issue for issue in issues))
                self.assertIn(evidence[0].split(maxsplit=1)[0], "\n".join(issues))

    def test_all_out_of_scope_paths_are_reported_individually(self) -> None:
        decision, _criteria, _blocking, issues = self.validate([
            "outside/other.py:1, ../escape.py:2 invalid",
        ])

        self.assertEqual("REVISE", decision.value)
        joined = "\n".join(issues)
        self.assertIn("outside/other.py:1", joined)
        self.assertIn("../escape.py:2", joined)

    def test_exhausted_attempts_remain_immutable_and_audited_after_rebaseline(self) -> None:
        helper = plan_revision_tests.HistoricalReviewRevisionTests(methodName="runTest")
        case, first, terminal = helper._case_with_revise_attempts(attempts=3)
        try:
            before = {
                path.relative_to(case.repo.root).as_posix(): path.read_bytes()
                for path in sorted((case.repo.root / ".cw/reviews").glob("*.json"))
            }
            self.assertEqual(3, case.repo.state()["attempt"])

            proposal = case.preview(reason="Add evidence normalization prerequisite")
            outcome = case.apply(proposal, operation_id="normalization-rebaseline")
            workflow = load_workflow(case.repo.root)
            state = load_state(case.repo.root)

            self.assertEqual(before, {
                reference: (case.repo.root / reference).read_bytes()
                for reference in before
            })
            self.assertEqual(3, state["attempt"])
            self.assertEqual(0, state["revision_attempt"])
            self.assertEqual("READY", state["status"])
            self.assertFalse((case.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
            self.assertIn(terminal, supersession_index(case.repo.root))

            for reference in before:
                _review_workflow, old_revision, superseded = review_revision(
                    case.repo.root,
                    workflow,
                    state,
                    reference,
                    load_json(case.repo.root / reference),
                )
                self.assertEqual(outcome["old_plan_revision_id"], old_revision)
                self.assertEqual(reference == terminal, superseded)

            self.assertIn(first, before)
            self.assertEqual(3, audit_history(case.repo.root, workflow, state)["reviews"])
            revision_audit = audit_revisions(case.repo.root, workflow, state)
            self.assertEqual(1, revision_audit["superseded_reviews"])
            self.assertEqual(
                proposal["new_plan_revision_id"], revision_audit["active_plan_revision"],
            )
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
