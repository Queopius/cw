from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from cw.agents.reviewer import run_review
from cw.cli.main import _doctor
from cw.core.audit import audit_history
from cw.core.completion import contract_hash, repository_snapshot
from cw.core.errors import CwError, ErrorCode
from cw.core.initialize import repair
from cw.core.state import load_state, save_state
from cw.core.utils import sha256_file
from cw.core.workflow import load_workflow, workflow_hash, write_workflow
from cw.planning.planner import Planner
from tests.helpers import FakeAdapter, TempRepo, result


class LegacyCompletionEvidenceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=2)
        plan = self.repo.root / ".codex/workflow/phases.yaml"
        document = json.loads(plan.read_text(encoding="utf-8"))
        document["completion_target"] = Planner.completion_contract(
            "Ready for a controlled customer pilot", target_type="controlled-pilot",
        )
        write_workflow(plan, document)
        self.repo.workflow = load_workflow(self.repo.root)
        state = self.repo.state()
        state["workflow_sha256"] = workflow_hash(plan)
        save_state(self.repo.root, state)
        for phase in range(1, 3):
            self.repo.artifact(phase)
            self.repo.ready(phase)
            run_review(
                self.repo.root,
                self.repo.workflow,
                self.repo.workflow.phases[phase - 1],
                self.repo.state(),
                FakeAdapter(result(phase)),
            )
        self.assertEqual("PLANNED_COMPLETE", self.repo.state()["status"])

    def tearDown(self) -> None:
        self.repo.close()

    def add_legacy_evidence(self) -> tuple[bytes, bytes]:
        completion = self.repo.root / ".cw/completion"
        logs = completion / "logs"
        reviews = completion / "reviews"
        logs.mkdir(parents=True)
        reviews.mkdir(exist_ok=True)
        log = logs / "completion-review.log"
        log.write_text("local completion diagnostics\n", encoding="utf-8")
        manifest = completion / "evidence_manifest.json"
        manifest.write_text(json.dumps({
            "kind": "local_completion_evidence_manifest",
            "entries": [{"path": "logs/completion-review.log", "sha256": sha256_file(log)}],
        }, indent=2) + "\n", encoding="utf-8")
        return log.read_bytes(), manifest.read_bytes()

    def test_repair_archives_known_legacy_completion_evidence_without_semantic_mutation(self) -> None:
        log_bytes, manifest_bytes = self.add_legacy_evidence()
        state_before = deepcopy(self.repo.state())
        history_before = deepcopy(state_before["history"])
        gate_bytes = {
            path.name: path.read_bytes() for path in sorted((self.repo.root / ".cw/gates").glob("*.json"))
        }
        contract = self.repo.workflow.completion_target
        assert contract is not None
        contract_before = contract_hash(contract)
        snapshot_before = repository_snapshot(self.repo.root)

        with self.assertRaises(CwError) as caught:
            audit_history(self.repo.root, self.repo.workflow, state_before)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, caught.exception.code)
        self.assertEqual("Unexpected completion evidence entry", caught.exception.message)
        before_checks = _doctor(self.repo.root, False)
        self.assertTrue(any(item["name"] == "Workflow integrity" and item["status"] == "error" for item in before_checks))

        report: dict = {}
        backup = repair(self.repo.root, report=report)

        self.assertTrue(backup.is_dir())
        self.assertEqual(log_bytes, (backup / "completion/logs/completion-review.log").read_bytes())
        self.assertEqual(manifest_bytes, (backup / "completion/evidence_manifest.json").read_bytes())
        self.assertEqual(2, len(report["legacy_completion_evidence"]))
        self.assertTrue(report["legacy_completion_evidence_validated"])
        self.assertFalse((self.repo.root / ".cw/completion/logs").exists())
        self.assertFalse((self.repo.root / ".cw/completion/evidence_manifest.json").exists())
        self.assertTrue((self.repo.root / ".cw/completion/reviews").is_dir())

        repaired_state = load_state(self.repo.root)
        self.assertEqual("PLANNED_COMPLETE", repaired_state["status"])
        self.assertEqual(history_before, repaired_state["history"])
        self.assertEqual(gate_bytes, {
            path.name: path.read_bytes() for path in sorted((self.repo.root / ".cw/gates").glob("*.json"))
        })
        self.assertEqual(contract_before, contract_hash(load_workflow(self.repo.root).completion_target))
        self.assertEqual(snapshot_before, repository_snapshot(self.repo.root))
        self.assertFalse((self.repo.root / ".cw/completion/completion.satisfied.json").exists())
        self.assertEqual([], list((self.repo.root / ".cw/completion/proposals").glob("*.json")))
        self.assertEqual([], list((self.repo.root / ".cw/completion/reviews").glob("*.json")))
        audit_history(self.repo.root, self.repo.workflow, repaired_state)

        with (
            patch("cw.cli.commands.read.shutil.which", return_value="/usr/bin/tool"),
            patch("cw.cli.commands.read.CodexAdapter.smoke_test", return_value=None),
        ):
            after_checks = _doctor(self.repo.root, True)
        self.assertFalse([item for item in after_checks if item["status"] == "error"], after_checks)
        self.assertTrue(any(item["name"] == "Reviewer connectivity" and item["status"] == "pass" for item in after_checks))

        second_report: dict = {}
        repair(self.repo.root, report=second_report)
        self.assertEqual([], second_report["legacy_completion_evidence"])
        self.assertEqual(history_before, load_state(self.repo.root)["history"])
        self.assertEqual(gate_bytes, {
            path.name: path.read_bytes() for path in sorted((self.repo.root / ".cw/gates").glob("*.json"))
        })

    def test_repair_fails_closed_for_unknown_completion_entry(self) -> None:
        self.add_legacy_evidence()
        unknown = self.repo.root / ".cw/completion/future-evidence.json"
        unknown.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as caught:
            repair(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, caught.exception.code)
        self.assertTrue(unknown.exists())
        self.assertTrue((self.repo.root / ".cw/completion/logs").exists())

    def test_repair_rejects_unsafe_or_malformed_known_legacy_evidence(self) -> None:
        completion = self.repo.root / ".cw/completion"
        manifest = completion / "evidence_manifest.json"
        manifest.write_text("[]\n", encoding="utf-8")
        with self.assertRaises(CwError) as malformed:
            repair(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, malformed.exception.code)
        self.assertTrue(manifest.exists())

        manifest.unlink()
        (completion / "logs").symlink_to(self.repo.root / "docs", target_is_directory=True)
        with self.assertRaises(CwError) as unsafe:
            repair(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, unsafe.exception.code)

    def test_repair_archives_known_legacy_evidence_without_completion_contract(self) -> None:
        legacy = TempRepo(phases=1)
        try:
            completion = legacy.root / ".cw/completion"
            completion.mkdir(exist_ok=True)
            (completion / "evidence_manifest.json").write_text('{"kind": "local"}\n', encoding="utf-8")
            report: dict = {}
            repair(legacy.root, report=report)
            self.assertEqual(1, len(report["legacy_completion_evidence"]))
            self.assertFalse((completion / "evidence_manifest.json").exists())
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
