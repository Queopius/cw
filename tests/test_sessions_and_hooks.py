from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.agents.reviewer import run_review
from cw.checks.deterministic import validate_phase
from cw.core.errors import CwError, ErrorCode
from cw.core.session import create_session, session_path
from tests.helpers import FakeAdapter, TempRepo, result


class SessionAndHookTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.phase = self.repo.workflow.phases[0]

    def tearDown(self):
        self.repo.close()

    def hook(self, environment: dict[str, str], payload: str = "not-json") -> subprocess.CompletedProcess[str]:
        hook = Path(__file__).parents[1] / "cw/templates/.codex/hooks/phase_gate.py"
        return subprocess.run(
            [sys.executable, str(hook)], input=payload, text=True,
            capture_output=True, env=environment, check=False,
        )

    def test_hook_is_inert_outside_cw_implementer(self):
        completed = self.hook(os.environ.copy())
        self.assertEqual(0, completed.returncode)
        self.assertEqual({}, json.loads(completed.stdout))

    def test_hook_is_inert_during_reviewer(self):
        environment = {**os.environ, "CW_IMPLEMENTER_ACTIVE": "1", "CW_REVIEWER_ACTIVE": "1"}
        completed = self.hook(environment)
        self.assertEqual({}, json.loads(completed.stdout))

    def test_hook_stops_recursive_delivery(self):
        environment = {**os.environ, "CW_IMPLEMENTER_ACTIVE": "1"}
        completed = self.hook(environment, json.dumps({
            "hook_event_name": "Stop", "stop_hook_active": True,
        }))
        output = json.loads(completed.stdout)
        self.assertFalse(output["continue"])
        self.assertIn("not recurse", output["stopReason"])

    def test_readiness_must_match_active_session(self):
        self.repo.artifact()
        create_session(self.repo.root, self.repo.workflow, self.phase)
        self.repo.ready(session_id="f" * 32)
        validation = validate_phase(self.repo.root, self.repo.workflow, self.phase)
        self.assertFalse(validation.passed)
        self.assertIn("active implementer session", validation.errors[0])

    def test_readiness_without_active_session_fails_closed(self):
        self.repo.artifact()
        self.repo.ready()
        session_path(self.repo.root).unlink()
        validation = validate_phase(self.repo.root, self.repo.workflow, self.phase)
        self.assertFalse(validation.passed)
        self.assertIn("no active implementer session", validation.errors[0])

    def test_readiness_requires_schema_version(self):
        self.repo.artifact()
        self.repo.ready()
        readiness = self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        payload = json.loads(readiness.read_text(encoding="utf-8"))
        payload.pop("schema_version")
        readiness.write_text(json.dumps(payload), encoding="utf-8")
        validation = validate_phase(self.repo.root, self.repo.workflow, self.phase)
        self.assertFalse(validation.passed)
        self.assertIn("schema version is invalid", validation.errors[0])

    def test_semantic_review_consumes_readiness_and_session(self):
        self.repo.artifact()
        create_session(self.repo.root, self.repo.workflow, self.phase)
        self.repo.ready()
        run_review(self.repo.root, self.repo.workflow, self.phase, self.repo.state(), FakeAdapter(result()))
        self.assertFalse(session_path(self.repo.root).exists())
        self.assertFalse((self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json").exists())

    def test_infrastructure_error_preserves_retryable_session(self):
        self.repo.artifact()
        session = create_session(self.repo.root, self.repo.workflow, self.phase)
        self.repo.ready()
        failure = CwError("network", ErrorCode.REVIEWER_NETWORK_ERROR)
        with self.assertRaises(CwError):
            run_review(
                self.repo.root, self.repo.workflow, self.phase, self.repo.state(),
                FakeAdapter(error=failure),
            )
        self.assertEqual(session["session_id"], json.loads(session_path(self.repo.root).read_text())["session_id"])
        self.assertTrue((self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json").exists())


if __name__ == "__main__":
    unittest.main()
