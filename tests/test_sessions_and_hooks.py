from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import io
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.agents.reviewer import run_review
from cw.cli.main import main
from cw.checks.deterministic import validate_phase
from cw.core.errors import CwError, ErrorCode
from cw.core.session import create_session, session_path
from cw.core.models import WorkflowState
from cw.core.state import load_state, save_state, transition
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

    def test_phase_gate_never_launches_planning(self):
        hook = Path(__file__).parents[1] / "cw/templates/.codex/hooks/phase_gate.py"
        source = hook.read_text(encoding="utf-8")
        self.assertNotIn('"plan"', source)
        self.assertNotIn("run_planner", source)

    def test_hook_stops_recursive_delivery(self):
        environment = {**os.environ, "CW_IMPLEMENTER_ACTIVE": "1"}
        completed = self.hook(environment, json.dumps({
            "hook_event_name": "Stop", "stop_hook_active": True,
        }))
        output = json.loads(completed.stdout)
        self.assertFalse(output["continue"])
        self.assertIn("not recurse", output["stopReason"])

    def test_stop_hook_path_does_not_internal_error_when_legacy_supersessions_are_absent(self):
        shutil.rmtree(self.repo.root / ".cw/supersessions")
        previous = Path.cwd()
        os.chdir(self.repo.root)
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                code = main(("review", "--hook"))
        finally:
            os.chdir(previous)
        self.assertNotIn("INTERNAL_ERROR", output.getvalue())
        self.assertIn(code, {0, 3})
        self.assertFalse((self.repo.root / ".cw/supersessions").exists())

    def test_readiness_must_match_active_session(self):
        self.repo.artifact()
        create_session(self.repo.root, self.repo.workflow, self.phase)
        self.repo.ready(session_id="f" * 32)
        validation = validate_phase(self.repo.root, self.repo.workflow, self.phase)
        self.assertFalse(validation.passed)
        self.assertIn("active implementer session", validation.errors[0])

    def test_parallel_implementer_session_is_rejected(self):
        create_session(self.repo.root, self.repo.workflow, self.phase)
        with self.assertRaises(CwError) as caught:
            create_session(self.repo.root, self.repo.workflow, self.phase)
        self.assertEqual(ErrorCode.LOCKED, caught.exception.code)

    def test_stale_implementer_session_requires_repair(self):
        create_session(self.repo.root, self.repo.workflow, self.phase)
        path = session_path(self.repo.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["owner_pid"] = 2_147_483_647
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CwError) as caught:
            create_session(self.repo.root, self.repo.workflow, self.phase)
        self.assertEqual(ErrorCode.INVALID_STATE, caught.exception.code)
        self.assertIn("repair", caught.exception.hint)

    def test_repair_removes_stale_session_without_readiness(self):
        from cw.core.initialize import repair

        create_session(self.repo.root, self.repo.workflow, self.phase)
        path = session_path(self.repo.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["owner_pid"] = 2_147_483_647
        path.write_text(json.dumps(payload), encoding="utf-8")
        repair(self.repo.root)
        self.assertFalse(path.exists())

    def test_readiness_without_active_session_fails_closed(self):
        self.repo.artifact()
        self.repo.ready()
        session_path(self.repo.root).unlink()
        validation = validate_phase(self.repo.root, self.repo.workflow, self.phase)
        self.assertFalse(validation.passed)
        self.assertIn("no active implementer session", validation.errors[0])

    def test_repair_rebinds_orphan_readiness_after_retained_revision(self):
        from cw.core.initialize import repair

        self.repo.artifact(content="first version\n")
        create_session(self.repo.root, self.repo.workflow, self.phase)
        self.repo.ready()
        report = run_review(
            self.repo.root,
            self.repo.workflow,
            self.phase,
            self.repo.state(),
            FakeAdapter(result(decision="REVISE", status="FAIL")),
        )
        self.assertEqual("REVISE", report["decision"])
        review_path = load_state(self.repo.root)["last_review"]
        review_bytes = (self.repo.root / review_path).read_bytes()

        self.repo.artifact(content="revised implementation\n")
        self.repo.ready()
        orphan = self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        original_orphan = orphan.read_bytes()
        session_path(self.repo.root).unlink()
        state = load_state(self.repo.root)
        state["last_error"] = "PROTECTED_PATH_MODIFIED: Semantic review evidence does not match repository state"
        state.setdefault("history", []).append({
            "timestamp": "2026-08-13T09:42:07Z",
            "phase": self.phase.id,
            "action": "protected_path_violation",
        })
        transition(self.repo.root, state, WorkflowState.ERROR)

        backup = repair(self.repo.root)
        repaired = load_state(self.repo.root)
        readiness = json.loads(orphan.read_text(encoding="utf-8"))
        session = json.loads(session_path(self.repo.root).read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_REVIEW", repaired["status"])
        self.assertEqual(1, repaired["attempt"])
        self.assertIsNone(repaired["last_error"])
        self.assertEqual(review_path, repaired["last_review"])
        self.assertEqual(session["session_id"], readiness["session_id"])
        self.assertNotEqual(original_orphan, orphan.read_bytes())
        self.assertEqual(review_bytes, (self.repo.root / review_path).read_bytes())
        self.assertEqual(original_orphan, (backup / "runtime/READY_FOR_REVIEW.json").read_bytes())
        self.assertTrue(validate_phase(self.repo.root, self.repo.workflow, self.phase).passed)

        session_before = session_path(self.repo.root).read_bytes()
        repair(self.repo.root)
        self.assertEqual(session_before, session_path(self.repo.root).read_bytes())

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
