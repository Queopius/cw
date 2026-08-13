from __future__ import annotations

import json
import unittest
from pathlib import Path

from cw.core.errors import CwError, ErrorCode
from cw.core.audit import audit_history
from cw.core.gates import artifact_hashes, validate_gate
from cw.core.initialize import repair
from cw.core.state import load_state, save_state
from cw.core.severity import CANONICAL_CRITERION_SEVERITIES, CriterionSeverity
from cw.core.workflow import load_workflow, write_workflow
from cw.core.utils import sha256_bytes
from cw.planning.planner import Planner
from tests.helpers import TempRepo


class CriterionSeverityCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(phases=1)
        self.plan_path = self.repo.root / ".codex/workflow/phases.yaml"

    def tearDown(self):
        self.repo.close()

    def plan(self):
        return json.loads(self.plan_path.read_text(encoding="utf-8"))

    def write_severities(self, values):
        plan = self.plan()
        plan["phases"][0]["acceptance_criteria"] = [
            {
                "id": f"CORE-{index:03d}",
                "description": (
                    "The canonical model remains intentionally small and its mapping boundary is documented."
                    if index == 4 else f"Core requirement {index}."
                ),
                "severity": severity,
            }
            for index, severity in enumerate(values, 1)
        ]
        write_workflow(self.plan_path, plan)

    def test_current_blocking_and_advisory_parse(self):
        for severity in ("blocking", "advisory"):
            with self.subTest(severity=severity):
                self.write_severities([severity])
                criterion = load_workflow(self.repo.root).phases[0].acceptance_criteria[0]
                self.assertEqual(severity, criterion.severity.value)

    def test_mixed_canonical_workflow_parses(self):
        self.write_severities(["blocking", "advisory"])
        criteria = load_workflow(self.repo.root).phases[0].acceptance_criteria
        self.assertEqual(["blocking", "advisory"], [item.severity.value for item in criteria])

    def test_unknown_severity_fails_closed(self):
        self.write_severities(["optional"])
        with self.assertRaises(CwError) as raised:
            load_workflow(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, raised.exception.code)

    def test_legacy_non_blocking_repair_is_backup_first_canonical_and_idempotent(self):
        self.write_severities(["blocking", "blocking", "blocking", "non-blocking"])
        original = self.plan_path.read_bytes()
        original_plan = self.plan()

        backup = repair(self.repo.root)

        migrated_bytes = self.plan_path.read_bytes()
        migrated = self.plan()
        criteria = migrated["phases"][0]["acceptance_criteria"]
        expected = json.loads(json.dumps(original_plan))
        expected["phases"][0]["acceptance_criteria"][3]["severity"] = "advisory"
        self.assertEqual(expected, migrated)
        self.assertEqual(
            ["blocking", "blocking", "blocking", "advisory"],
            [item["severity"] for item in criteria],
        )
        self.assertNotEqual("blocking", criteria[3]["severity"])
        for key in ("id", "description"):
            self.assertEqual(
                [item[key] for item in original_plan["phases"][0]["acceptance_criteria"]],
                [item[key] for item in criteria],
            )
        self.assertEqual(original, (backup / "phases.yaml").read_bytes())
        self.assertNotIn('"severity": "non-blocking"', migrated_bytes.decode("utf-8"))

        second_backup = repair(self.repo.root)

        self.assertNotEqual(backup, second_backup)
        self.assertEqual(migrated_bytes, self.plan_path.read_bytes())
        self.assertEqual("advisory", load_workflow(self.repo.root).phases[0].acceptance_criteria[3].severity.value)

    def test_planner_generates_only_canonical_severities(self):
        plan = Planner().propose_plan(self.repo.root, "sample-app", "Implement webhook delivery")
        values = {
            criterion["severity"]
            for phase in plan["phases"]
            for criterion in phase["acceptance_criteria"]
        }
        self.assertTrue(values)
        self.assertLessEqual(values, CANONICAL_CRITERION_SEVERITIES)
        self.assertNotIn("non-blocking", values)

    def test_plan_schema_enum_matches_python_enum(self):
        from cw import __file__ as package_file

        schema = json.loads(
            (Path(package_file).parent / "schemas/plan-proposal.schema.json").read_text(encoding="utf-8")
        )
        serialized = schema["properties"]["phases"]["items"]["properties"]["acceptance_criteria"]["items"]["properties"]["severity"]["enum"]
        self.assertEqual(set(CANONICAL_CRITERION_SEVERITIES), set(serialized))
        self.assertEqual({item.value for item in CriterionSeverity}, set(serialized))

    def test_legacy_review_and_gate_remain_immutable_and_validate_after_migration(self):
        self.write_severities(["blocking", "blocking", "blocking", "non-blocking"])
        repair(self.repo.root)
        workflow = load_workflow(self.repo.root)
        phase = workflow.phases[0]
        self.repo.artifact()
        hashes = artifact_hashes(self.repo.root, phase.artifacts)
        review_path = self.repo.root / ".cw/reviews/01-phase-1-attempt-04.json"
        review = {
            "schema_version": 1,
            "cw_version": "0.1.2",
            "timestamp": "2026-08-12T13:37:25Z",
            "workflow_id": workflow.id,
            "workflow_version": workflow.version,
            "workflow_sha256": "sha256:" + "1" * 64,
            "phase": phase.id,
            "attempt": 1,
            "review_sequence": 4,
            "reviewer_result": {
                "decision": "APPROVE",
                "next_phase_allowed": True,
                "summary": "Legacy approval",
                "criteria": [
                    {
                        "id": criterion.id,
                        "passed": True,
                        "severity": "non-blocking" if criterion.id == "CORE-004" else "blocking",
                        "evidence": ["docs/phase-1.md:1 evidence"],
                        "explanation": "verified",
                    }
                    for criterion in phase.acceptance_criteria
                ],
                "blocking_issues": [],
                "non_blocking_observations": [],
            },
            "artifact_hashes": hashes,
            "reviewed_files": hashes,
            "final_decision": "APPROVE",
            "system_error": None,
        }
        review_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
        pre_schema_review = dict(review)
        pre_schema_review.pop("schema_version")
        pre_schema_review.pop("cw_version")
        pre_schema_hash = sha256_bytes(
            (json.dumps(pre_schema_review, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
        gate_path = self.repo.root / ".cw/gates/01-phase-1.approved.json"
        gate = {
            "schema_version": 1,
            "cw_version": "0.1.2",
            "workflow_id": workflow.id,
            "workflow_version": workflow.version,
            "workflow_sha256": "sha256:" + "1" * 64,
            "phase": phase.id,
            "decision": "APPROVED",
            "approval_type": "independent-review",
            "approved_at": "2026-08-12T13:37:25Z",
            "review_attempt": 1,
            "review_file": review_path.relative_to(self.repo.root).as_posix(),
            "review_sha256": pre_schema_hash,
            "artifacts": hashes,
            "reviewed_files": hashes,
        }
        gate_path.write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
        original_review = review_path.read_bytes()
        original_gate = gate_path.read_bytes()
        state = load_state(self.repo.root)
        state["last_review"] = review_path.relative_to(self.repo.root).as_posix()
        state["last_gate"] = gate_path.relative_to(self.repo.root).as_posix()
        save_state(self.repo.root, state)
        (self.repo.root / ".cw/gates/archive/20260812T133014Z").mkdir(parents=True)

        validate_gate(self.repo.root, workflow, phase.id)
        self.assertEqual({"reviews": 1, "gates": 1, "events": 0}, audit_history(self.repo.root, workflow, state))
        self.assertEqual(original_review, review_path.read_bytes())
        self.assertEqual(original_gate, gate_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
