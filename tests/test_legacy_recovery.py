from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.adapters.codex import CodexResult
from cw.cli.main import main
from cw.core.gates import create_gate, validate_gate
from cw.core.initialize import repair
from cw.core.recovery import classify_legacy_infrastructure_error
from cw.core.session import create_session, readiness_path
from cw.core.state import load_state, save_state
from cw.core.utils import atomic_json
from cw.core.workflow import load_workflow, workflow_hash, write_workflow
from tests.helpers import TempRepo, result


OLD_TIMESTAMP = "2025-11-04T09:15:00Z"


class LegacyReviewerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(phases=2)
        self.previous = Path.cwd()
        os.chdir(self.repo.root)

    def tearDown(self):
        os.chdir(self.previous)
        self.repo.close()

    def invoke(self, *args):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args)
        return code, stream.getvalue()

    def prepare_legacy_error(self, *, readiness: bool) -> tuple[Path, Path, bytes]:
        self.repo.artifact(1)
        first_review = self.repo.approved_review(1)
        first_gate = create_gate(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], first_review,
        )
        gate_bytes = first_gate.read_bytes()

        plan_path = self.repo.root / ".codex/workflow/phases.yaml"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["phases"][1]["id"] = "06-first-e2e"
        plan["phases"][1]["name"] = "First E2E"
        write_workflow(plan_path, plan)
        self.repo.workflow = load_workflow(self.repo.root)
        phase = self.repo.workflow.phase("06-first-e2e")
        self.repo.artifact(2)

        legacy_review = self.repo.root / ".cw/reviews/06-first-e2e-attempt-01.json"
        atomic_json(legacy_review, {
            "schema_version": 1,
            "workflow": self.repo.workflow.id,
            "phase": phase.id,
            "attempt": 1,
            "reviewer_result": None,
            "system_error": {
                "timestamp": OLD_TIMESTAMP,
                "message": "Reviewer smoke test failed: Operation not permitted",
            },
            "created_at": OLD_TIMESTAMP,
        })

        state = load_state(self.repo.root)
        state.update({
            "workflow_version": self.repo.workflow.version,
            "workflow_sha256": workflow_hash(plan_path),
            "current_phase": phase.id,
            "status": "ERROR",
            "attempt": 1,
            "last_review": legacy_review.relative_to(self.repo.root).as_posix(),
            "last_gate": first_gate.relative_to(self.repo.root).as_posix(),
            "last_error": (
                f"{OLD_TIMESTAMP} Reviewer smoke test failed\n"
                "Operation not permitted while starting external reviewer"
            ),
            "infrastructure_error": None,
        })
        save_state(self.repo.root, state)
        if readiness:
            session = create_session(self.repo.root, self.repo.workflow, phase)
            atomic_json(readiness_path(self.repo.root), {
                "schema_version": 1,
                "session_id": session["session_id"],
                "phase": phase.id,
                "status": "READY_FOR_REVIEW",
                "artifacts": list(phase.artifacts),
                "checks_executed": [],
            })
        return legacy_review, first_gate, gate_bytes

    def test_repair_and_retry_recover_legacy_review_without_implementation(self):
        legacy_review, first_gate, gate_bytes = self.prepare_legacy_error(readiness=True)
        code, repair_output = self.invoke("repair", "--json")
        self.assertEqual(0, code, repair_output)
        backup = self.repo.root / json.loads(repair_output)["backup"]

        repaired = load_state(self.repo.root)
        self.assertEqual(0, repaired["attempt"])
        self.assertEqual("READY_FOR_REVIEW", repaired["status"])
        self.assertIsNone(repaired["last_error"])
        self.assertEqual("REVIEWER_PROCESS_ERROR", repaired["infrastructure_error"]["error_code"])
        self.assertEqual("review", repaired["infrastructure_error"]["operation"])
        self.assertTrue(repaired["infrastructure_error"]["retryable"])
        self.assertEqual(gate_bytes, first_gate.read_bytes())
        self.assertEqual("REVIEWER_PROCESS_ERROR", json.loads(legacy_review.read_text())["error_code"])
        original = json.loads((backup / "reviews/06-first-e2e-attempt-01.json").read_text())
        self.assertIsNone(original["reviewer_result"])
        self.assertIsNotNone(original["system_error"])

        code, status_output = self.invoke("status")
        self.assertEqual(0, code)
        self.assertIn("READY_FOR_REVIEW", status_output)
        self.assertNotIn(OLD_TIMESTAMP, status_output)

        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
            return_value=CodexResult(result(2), ""),
        ) as reviewer:
            code, output = self.invoke("retry", "--json")

        self.assertEqual(0, code, output)
        implementer.assert_not_called()
        reviewer.assert_called_once()
        final = load_state(self.repo.root)
        self.assertEqual("06-first-e2e", final["current_phase"])
        self.assertEqual("COMPLETED", final["status"])
        self.assertEqual(1, final["attempt"])
        retry_event = next(event for event in final["history"] if event["action"] == "retry_started")
        approval_event = next(event for event in final["history"] if event["action"] == "approved" and event["phase"] == "06-first-e2e")
        self.assertEqual("review", retry_event["operation"])
        self.assertNotEqual(OLD_TIMESTAMP, retry_event["timestamp"])
        self.assertNotEqual(OLD_TIMESTAMP, approval_event["timestamp"])
        semantic = [
            json.loads(path.read_text())
            for path in (self.repo.root / ".cw/reviews").glob("06-first-e2e-*.json")
            if json.loads(path.read_text()).get("kind") == "semantic_review"
        ]
        self.assertEqual(1, len(semantic))
        self.assertNotEqual(OLD_TIMESTAMP, semantic[0]["created_at"])
        validate_gate(self.repo.root, self.repo.workflow, "01-phase-1")

    def test_retry_regenerates_only_readiness_when_manifest_is_missing(self):
        self.prepare_legacy_error(readiness=False)
        repair(self.repo.root)
        repaired = load_state(self.repo.root)
        self.assertEqual("ERROR", repaired["status"])
        self.assertEqual(0, repaired["attempt"])

        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
            return_value=CodexResult(result(2), ""),
        ) as reviewer:
            code, output = self.invoke("retry", "--json")

        self.assertEqual(0, code, output)
        implementer.assert_not_called()
        reviewer.assert_called_once()
        actions = [event["action"] for event in load_state(self.repo.root)["history"]]
        self.assertIn("readiness_resume_started", actions)

    def test_retry_fails_closed_without_readiness_or_completed_artifacts(self):
        self.prepare_legacy_error(readiness=False)
        (self.repo.root / "docs/phase-2.md").unlink()
        repair(self.repo.root)

        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
        ) as reviewer:
            code, output = self.invoke("retry", "--json")

        self.assertEqual(1, code)
        self.assertEqual("INVALID_STATE", json.loads(output)["error"]["code"])
        implementer.assert_not_called()
        reviewer.assert_not_called()
        state = load_state(self.repo.root)
        self.assertEqual("ERROR", state["status"])
        self.assertEqual("06-first-e2e", state["current_phase"])

    def test_direct_retry_migrates_legacy_error_before_review(self):
        legacy_review, _, _ = self.prepare_legacy_error(readiness=True)

        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
            return_value=CodexResult(result(2), ""),
        ) as reviewer:
            code, output = self.invoke("retry", "--json")

        self.assertEqual(0, code, output)
        implementer.assert_not_called()
        reviewer.assert_called_once()
        self.assertEqual("infrastructure_error", json.loads(legacy_review.read_text())["kind"])
        backups = list((self.repo.root / ".cw/backups").iterdir())
        self.assertEqual(1, len(backups))
        original = json.loads((backups[0] / "reviews" / legacy_review.name).read_text())
        self.assertIsNone(original["reviewer_result"])


class LegacyInfrastructureClassificationTests(unittest.TestCase):
    def test_known_reviewer_infrastructure_signatures(self):
        cases = {
            "Reviewer smoke test failed": "REVIEWER_PROCESS_ERROR",
            "Operation not permitted": "REVIEWER_PROCESS_ERROR",
            "websocket transport error": "REVIEWER_NETWORK_ERROR",
            "reviewer process crash": "REVIEWER_PROCESS_ERROR",
            "reviewer timeout": "REVIEW_TIMEOUT",
            "invalid response schema": "SCHEMA_VALIDATION_ERROR",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                metadata = classify_legacy_infrastructure_error(
                    message, phase="06-first-e2e", operation="review",
                )
                self.assertIsNotNone(metadata)
                self.assertEqual(expected, metadata["error_code"])
                self.assertEqual("review", metadata["operation"])
                self.assertTrue(metadata["retryable"])
                self.assertTrue(metadata["legacy"])


if __name__ == "__main__":
    unittest.main()
