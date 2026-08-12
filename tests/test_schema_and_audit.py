from __future__ import annotations

import json
import unittest

from cw.cli.main import _doctor
from cw.core.audit import audit_history
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import create_gate
from cw.core.initialize import initialize, repair
from cw.core.project import load_project
from cw.core.schema import schema_version
from cw.core.state import load_state
from cw.core.workflow import load_workflow
from tests.helpers import TempRepo


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class SchemaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()

    def tearDown(self):
        self.repo.close()

    def test_future_project_schema_fails_closed(self):
        path = self.repo.root / ".cw/project.json"
        data = _json(path); data["schema_version"] = 2; _write(path, data)
        with self.assertRaises(CwError) as caught:
            load_project(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VERSION_ERROR, caught.exception.code)

    def test_init_rejects_foreign_legacy_identity_before_migration(self):
        path = self.repo.root / ".cw/project.json"
        data = _json(path); data.pop("schema_version"); data["project_id"] = "foreign"; _write(path, data)
        before = path.read_bytes()
        with self.assertRaises(CwError) as caught:
            initialize(self.repo.root)
        self.assertEqual(ErrorCode.WORKFLOW_PROJECT_MISMATCH, caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_future_state_and_workflow_schemas_fail_closed(self):
        state_path = self.repo.root / ".cw/state.json"
        state = _json(state_path); state["schema_version"] = 2; _write(state_path, state)
        with self.assertRaises(CwError) as state_error:
            load_state(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VERSION_ERROR, state_error.exception.code)

        plan_path = self.repo.root / ".codex/workflow/phases.yaml"
        plan = _json(plan_path); plan["schema_version"] = 2; _write(plan_path, plan)
        with self.assertRaises(CwError) as plan_error:
            load_workflow(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VERSION_ERROR, plan_error.exception.code)

    def test_boolean_schema_version_is_rejected(self):
        with self.assertRaises(CwError) as caught:
            schema_version({"schema_version": True}, "Fixture")
        self.assertEqual(ErrorCode.SCHEMA_VERSION_ERROR, caught.exception.code)

    def test_repair_backs_up_but_does_not_overwrite_future_state(self):
        path = self.repo.root / ".cw/state.json"
        data = _json(path); data["schema_version"] = 2; _write(path, data)
        before = path.read_bytes()
        with self.assertRaises(CwError) as caught:
            repair(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VERSION_ERROR, caught.exception.code)
        self.assertEqual(before, path.read_bytes())
        backups = list((self.repo.root / ".cw/backups").iterdir())
        self.assertEqual(1, len(backups))
        self.assertEqual(before, (backups[0] / "state.json").read_bytes())

    def test_repair_migrates_all_schema_less_historical_documents(self):
        self.repo.artifact()
        review_reference = self.repo.approved_review()
        gate = create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], review_reference)
        paths = [
            self.repo.root / ".cw/project.json",
            self.repo.root / ".cw/state.json",
            self.repo.root / ".codex/workflow/phases.yaml",
            self.repo.root / review_reference,
            gate,
        ]
        for path in paths:
            data = _json(path); data.pop("schema_version"); _write(path, data)

        repair(self.repo.root)

        for path in paths:
            self.assertEqual(1, _json(path)["schema_version"], path.name)
        workflow = load_workflow(self.repo.root)
        result = audit_history(self.repo.root, workflow, load_state(self.repo.root))
        self.assertEqual({"reviews": 1, "gates": 1, "events": 0}, result)


class HistoricalAuditTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.repo.artifact()
        self.review_reference = self.repo.approved_review()

    def tearDown(self):
        self.repo.close()

    def audit(self):
        return audit_history(self.repo.root, self.repo.workflow, load_state(self.repo.root))

    def test_valid_review_and_gate_history(self):
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], self.review_reference)
        self.assertEqual({"reviews": 1, "gates": 1, "events": 0}, self.audit())

    def test_tampered_historical_review_fails_audit_and_doctor(self):
        path = self.repo.root / self.review_reference
        data = _json(path); data["criteria"][0]["id"] = "INVENTED"; _write(path, data)
        with self.assertRaises(CwError):
            self.audit()
        checks = _doctor(self.repo.root, False)
        failure = next(item for item in checks if item["name"] == "Workflow integrity")
        self.assertEqual("error", failure["status"])

    def test_unknown_gate_file_fails_audit(self):
        _write(self.repo.root / ".cw/gates/unknown.approved.json", {"schema_version": 1})
        with self.assertRaises(CwError) as caught:
            self.audit()
        self.assertEqual(ErrorCode.INVALID_GATE, caught.exception.code)

    def test_unknown_history_action_fails_audit(self):
        state = _json(self.repo.root / ".cw/state.json")
        state["history"].append({"timestamp": "2026-08-12T00:00:00Z", "phase": "01-phase-1", "action": "invented"})
        _write(self.repo.root / ".cw/state.json", state)
        with self.assertRaises(CwError) as caught:
            self.audit()
        self.assertEqual(ErrorCode.INVALID_STATE, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
