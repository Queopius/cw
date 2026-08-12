from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.cli.main import main
from cw.core.models import WorkflowState
from cw.core.state import save_state, transition
from cw.ui.console import Console
from tests.helpers import TempRepo


class Tty(io.StringIO):
    def isatty(self): return True


class CliTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
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

    def test_status_healthy(self):
        code, output = self.invoke("status")
        self.assertEqual(0, code)
        self.assertIn("IN_PROGRESS", output)
        self.assertIn("01  Phase 1", output)

    def test_default_command_is_start(self):
        with patch("cw.cli.main.CodexAdapter.run_implementer", return_value=0) as implementer:
            code, output = self.invoke()
        self.assertEqual(0, code)
        self.assertIn("Phase 1", output)
        implementer.assert_called_once()

    def test_help_flag_uses_public_help(self):
        code, output = self.invoke("--help")
        self.assertEqual(0, code)
        self.assertIn("CW by Queopius · Codex Workflow", output)

    def test_status_json_valid(self):
        code, output = self.invoke("status", "--json")
        self.assertEqual(0, code)
        self.assertEqual("sample-app", json.loads(output)["project"])
        self.assertNotIn("\033[", output)

    def test_status_error_is_compact(self):
        state = self.repo.state(); state["last_error"] = "REVIEWER_NETWORK_ERROR: failed\n" + "trace\n" * 50
        transition(self.repo.root, state, WorkflowState.ERROR, force_error=True)
        code, output = self.invoke("status")
        self.assertEqual(1, code)
        self.assertIn("Reviewer unavailable", output)
        self.assertNotIn("trace", output)

    def test_error_full_detail(self):
        state = self.repo.state(); state["last_error"] = "REVIEWER_NETWORK_ERROR: failed\nfull diagnostic"
        save_state(self.repo.root, state)
        code, output = self.invoke("error")
        self.assertEqual(1, code)
        self.assertIn("full diagnostic", output)

    def test_doctor_healthy(self):
        with patch("cw.cli.main.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
            code, output = self.invoke("doctor")
        self.assertEqual(0, code)
        self.assertIn("checks passed", output)

    def test_doctor_json(self):
        code, output = self.invoke("doctor", "--json")
        self.assertIn("checks", json.loads(output))

    def test_doctor_warning(self):
        checks = [{"section": "Security", "name": "Hook trust", "status": "warning", "detail": "review required"}]
        with patch("cw.cli.main._doctor", return_value=checks):
            code, output = self.invoke("doctor")
        self.assertEqual(0, code)
        self.assertIn("1 warnings", output)

    def test_doctor_failure(self):
        checks = [{"section": "Workflow", "name": "State", "status": "error", "detail": "invalid"}]
        with patch("cw.cli.main._doctor", return_value=checks):
            code, output = self.invoke("doctor")
        self.assertEqual(1, code)
        self.assertIn("1 errors", output)

    def test_history_empty(self):
        code, output = self.invoke("history")
        self.assertEqual(0, code)
        self.assertIn("No workflow events", output)

    def test_version_json(self):
        code, output = self.invoke("version", "--json")
        payload = json.loads(output)
        self.assertEqual("0.1.0", payload["version"])
        self.assertEqual("CW by Queopius", payload["brand"])

    def test_no_color_environment(self):
        stream = Tty()
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            console = Console(stream=stream); console.item("✓", "ok")
        self.assertNotIn("\033[", stream.getvalue())

    def test_tty_color(self):
        stream = Tty()
        with patch.dict(os.environ, {}, clear=True):
            console = Console(stream=stream); console.item("✓", "ok")
        self.assertIn("\033[", stream.getvalue())

    def test_non_tty_no_color(self):
        stream = io.StringIO(); Console(stream=stream).item("✓", "ok")
        self.assertNotIn("\033[", stream.getvalue())

    def test_verbose_status(self):
        _, output = self.invoke("status", "--verbose")
        self.assertIn(str(self.repo.root), output)
        self.assertIn(".cw/state.json", output)

    def test_unclear_plan_can_be_retried_with_goal(self):
        # Reset the fixture to the initialized/no-plan lifecycle.
        from cw.core.state import initial_state
        from cw.core.workflow import write_workflow
        project_id = "sample-app"
        write_workflow(self.repo.root / ".codex/workflow/phases.yaml", {
            "schema_version": 1,
            "workflow": {"id": project_id, "repository": project_id, "version": 1, "status": "NOT_CREATED", "goal": None},
            "settings": {}, "reviewer": {}, "phases": [],
        })
        save_state(self.repo.root, initial_state(project_id))
        code, output = self.invoke("plan")
        self.assertEqual(1, code)
        self.assertIn("Project goal is unclear", output)
        code, output = self.invoke("plan", "--goal", "Build a webhook handler", "--json")
        self.assertEqual(0, code)
        self.assertEqual("PROPOSED", json.loads(output)["status"])

    def test_explicit_reopen_backs_up_and_invalidates_dependent_gates(self):
        from cw.core.gates import create_gate
        self.repo.artifact(1); self.repo.artifact(2)
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], "review-1")
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[1], "review-2")
        code, output = self.invoke("repair", "--reopen", "01-phase-1")
        self.assertEqual(0, code)
        self.assertIn("Phase reopened", output)
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
        self.assertFalse((self.repo.root / ".cw/gates/02-phase-2.approved.json").exists())
        self.assertEqual("01-phase-1", self.repo.state()["current_phase"])
        self.assertTrue(list((self.repo.root / ".cw/backups").glob("*/gates/01-phase-1.approved.json")))


if __name__ == "__main__":
    unittest.main()
