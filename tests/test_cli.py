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

from cw import __version__
from cw.adapters.codex import CodexResult
from cw.cli.main import _context, main
from cw.core.errors import CwError, ErrorCode
from cw.core.models import WorkflowState
from cw.core.state import save_state, transition
from cw.ui.console import Console
from cw.planning.planner import Planner
from tests.helpers import TempRepo, result


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
        def implement(*_args, **_kwargs):
            self.repo.artifact(); self.repo.ready(); return 0

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement) as implementer:
            code, output = self.invoke()
        self.assertEqual(0, code)
        self.assertIn("Phase 1", output)
        implementer.assert_called_once()

    def test_start_passes_effective_network_policy(self):
        (self.repo.root / ".cw/config.toml").write_text("allow_network = true\n", encoding="utf-8")
        def implement(*_args, **_kwargs):
            self.repo.artifact(); self.repo.ready(); return 0

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement) as implementer:
            code, _ = self.invoke("start")
        self.assertEqual(0, code)
        self.assertTrue(implementer.call_args.kwargs["allow_network"])

    def test_start_allows_review_and_gate_created_by_hook(self):
        def implement(root, prompt, **kwargs):
            from cw.agents.reviewer import run_review
            from tests.helpers import FakeAdapter, result
            self.repo.artifact()
            self.repo.ready()
            run_review(root, self.repo.workflow, self.repo.workflow.phases[0], self.repo.state(), FakeAdapter(result()))
            return 0

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement):
            code, _ = self.invoke("start")
        self.assertEqual(0, code)
        self.assertEqual("IN_PROGRESS", self.repo.state()["status"])
        self.assertEqual("02-phase-2", self.repo.state()["current_phase"])
        self.assertTrue((self.repo.root / ".cw/gates/01-phase-1.approved.json").is_file())

    def test_start_fails_closed_when_implementer_mutates_state(self):
        def implement(root, prompt, **kwargs):
            state = self.repo.state()
            state["status"] = "APPROVED"
            save_state(root, state)
            return 0

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement):
            code, output = self.invoke("start")
        self.assertEqual(1, code)
        self.assertIn("Protected workflow metadata changed", output)
        self.assertEqual("ERROR", self.repo.state()["status"])
        self.assertIn("PROTECTED_PATH_MODIFIED", self.repo.state()["last_error"])

    def test_start_recovers_known_good_state_when_implementer_corrupts_json(self):
        def implement(root, prompt, **kwargs):
            (root / ".cw/state.json").write_text("{", encoding="utf-8")
            return 0

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement):
            code, output = self.invoke("start")
        self.assertEqual(1, code)
        self.assertIn("Protected workflow metadata changed", output)
        state = self.repo.state()
        self.assertEqual("ERROR", state["status"])
        self.assertEqual("protected_path_violation", state["history"][-1]["action"])

    def test_implementer_failure_enters_retryable_error_state(self):
        failure = CwError("implementer failed", ErrorCode.IMPLEMENTER_PROCESS_ERROR, "Run: cw retry", details="exit 9")
        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=failure):
            code, output = self.invoke("start")
        self.assertEqual(1, code)
        self.assertIn("Implementer stopped unexpectedly", output)
        self.assertNotIn("exit 9", output)
        state = self.repo.state()
        self.assertEqual("ERROR", state["status"])
        self.assertIn("IMPLEMENTER_PROCESS_ERROR", state["last_error"])

    def test_retry_restarts_only_failed_implementer(self):
        state = self.repo.state()
        state["last_error"] = "IMPLEMENTER_PROCESS_ERROR: exited"
        transition(self.repo.root, state, WorkflowState.ERROR, force_error=True)
        def implement(*_args, **_kwargs):
            self.repo.artifact(); self.repo.ready(); return 0

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement) as implementer:
            code, _ = self.invoke("retry")
        self.assertEqual(0, code)
        implementer.assert_called_once()
        self.assertEqual("IN_PROGRESS", self.repo.state()["status"])

    def test_start_without_readiness_enters_retryable_error(self):
        with patch("cw.cli.main.CodexAdapter.run_implementer", return_value=0):
            code, output = self.invoke("start")
        self.assertEqual(1, code)
        self.assertIn("Implementer stopped unexpectedly", output)
        self.assertEqual("ERROR", self.repo.state()["status"])
        self.assertFalse((self.repo.root / ".cw/runtime/implementer-session.json").exists())

    def test_start_json_is_rejected_without_mutating_state(self):
        before = (self.repo.root / ".cw/state.json").read_bytes()
        code, output = self.invoke("start", "--json")
        self.assertEqual(2, code)
        self.assertEqual("USAGE_ERROR", json.loads(output)["error"]["code"])
        self.assertEqual(before, (self.repo.root / ".cw/state.json").read_bytes())

    def test_retry_reviews_existing_readiness_after_implementer_exit(self):
        failure = CwError("exited after readiness", ErrorCode.IMPLEMENTER_PROCESS_ERROR, "Run: cw retry")

        def implement(root, prompt, **kwargs):
            self.repo.artifact()
            self.repo.ready()
            raise failure

        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement):
            code, _ = self.invoke("start")
        self.assertEqual(1, code)
        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer", return_value=CodexResult(result(), "")
        ):
            code, _ = self.invoke("retry")
        self.assertEqual(0, code)
        implementer.assert_not_called()
        self.assertEqual("IN_PROGRESS", self.repo.state()["status"])
        self.assertEqual("02-phase-2", self.repo.state()["current_phase"])

    def test_hook_review_rejects_wrong_session_environment(self):
        from cw.core.session import create_session
        session = create_session(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0])
        self.repo.artifact()
        self.repo.ready()
        with patch.dict(os.environ, {
            "CW_IMPLEMENTER_ACTIVE": "1", "CW_IMPLEMENTER_SESSION": "f" * 32,
        }), patch("cw.cli.main.run_review") as reviewer:
            code, output = self.invoke("review", "--hook")
        self.assertEqual(0, code)
        self.assertEqual({}, json.loads(output))
        reviewer.assert_not_called()
        self.assertNotEqual("f" * 32, session["session_id"])

    def test_hook_revision_stops_instead_of_requesting_continuation(self):
        from cw.core.session import create_session
        session = create_session(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0])
        self.repo.artifact()
        self.repo.ready()
        with patch.dict(os.environ, {
            "CW_IMPLEMENTER_ACTIVE": "1", "CW_IMPLEMENTER_SESSION": session["session_id"],
        }), patch("cw.cli.main.run_review", return_value={"decision": "REVISE", "blocking_issues": ["fix"]}):
            code, output = self.invoke("review", "--hook")
        self.assertEqual(0, code)
        payload = json.loads(output)
        self.assertFalse(payload["continue"])
        self.assertNotIn("decision", payload)

    def test_human_review_command_reports_created_gate(self):
        gate = self.repo.root / ".cw/gates/01-phase-1.approved.json"
        with patch("cw.cli.main.human_approve", return_value=gate) as approver:
            code, output = self.invoke("review", "--human-approve", "--json")
        self.assertEqual(0, code)
        self.assertEqual({
            "decision": "APPROVE",
            "gate": ".cw/gates/01-phase-1.approved.json",
            "human": True,
            "next_phase": "02-phase-2",
            "workflow_completed": False,
        }, json.loads(output))
        approver.assert_called_once()

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
        with patch("cw.cli.commands.read.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
            code, output = self.invoke("doctor")
        self.assertEqual(0, code)
        self.assertIn("checks passed", output)
        self.assertIn("Implementer session", output)

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
        self.assertIn("Current", output)

    def test_version_json(self):
        code, output = self.invoke("version", "--json")
        payload = json.loads(output)
        self.assertEqual(__version__, payload["version"])
        self.assertEqual("CW by Queopius", payload["brand"])

    def test_global_json_flag_before_command(self):
        code, output = self.invoke("--json", "version")
        self.assertEqual(0, code)
        self.assertEqual("CW by Queopius", json.loads(output)["brand"])

    def test_config_set_updates_project_policy_atomically(self):
        code, output = self.invoke("config", "set", "allow_network", "true", "--json")
        payload = json.loads(output)
        self.assertEqual(0, code)
        self.assertEqual("project", payload["scope"])
        self.assertTrue(payload["value"])
        self.assertIn("allow_network = true", (self.repo.root / ".cw/config.toml").read_text(encoding="utf-8"))
        self.assertTrue(_context(self.repo.root)[2].allow_network)

    def test_config_set_accepts_string_lists(self):
        value = '["payments", "cryptography"]'
        code, output = self.invoke("config", "set", "human_gate_categories", value, "--json")
        self.assertEqual(0, code)
        self.assertEqual(["payments", "cryptography"], json.loads(output)["value"])
        self.assertEqual(("payments", "cryptography"), _context(self.repo.root)[2].human_gate_categories)

    def test_config_set_rejects_unknown_key_without_modifying_file(self):
        path = self.repo.root / ".cw/config.toml"
        before = path.read_bytes()
        code, output = self.invoke("config", "set", "secret_mode", "true")
        self.assertEqual(2, code)
        self.assertIn("Unknown configuration setting", output)
        self.assertEqual(before, path.read_bytes())

    def test_config_set_rejects_unsafe_path_without_modifying_file(self):
        path = self.repo.root / ".cw/config.toml"
        before = path.read_bytes()
        code, output = self.invoke("config", "set", "protected_paths", '["../outside"]')
        self.assertEqual(2, code)
        self.assertIn("must be repository-relative", output)
        self.assertEqual(before, path.read_bytes())

    def test_config_set_rejects_non_positive_integer_without_modifying_file(self):
        path = self.repo.root / ".cw/config.toml"
        before = path.read_bytes()
        code, output = self.invoke("config", "set", "review_timeout", "0")
        self.assertEqual(2, code)
        self.assertIn("must be a positive integer", output)
        self.assertEqual(before, path.read_bytes())

    def test_config_set_requires_key_and_value(self):
        code, output = self.invoke("config", "set")
        self.assertEqual(2, code)
        self.assertIn("setting and value are required", output)

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
        proposal = Planner().propose_plan(self.repo.root, project_id, "Build a webhook handler")
        with patch("cw.cli.main.CodexAdapter.run_planner", return_value=CodexResult({"phases": proposal["phases"]}, "")):
            code, output = self.invoke("plan", "--goal", "Build a webhook handler", "--json")
        self.assertEqual(0, code)
        self.assertEqual("PROPOSED", json.loads(output)["status"])

    def test_planner_infrastructure_failure_is_retryable_without_losing_goal(self):
        from cw.core.state import initial_state
        from cw.core.workflow import write_workflow
        project_id = "sample-app"
        write_workflow(self.repo.root / ".codex/workflow/phases.yaml", {
            "schema_version": 1,
            "workflow": {"id": project_id, "repository": project_id, "version": 1, "status": "NOT_CREATED", "goal": None},
            "settings": {}, "reviewer": {}, "phases": [],
        })
        save_state(self.repo.root, initial_state(project_id))
        failure = CwError("planner offline", ErrorCode.PLANNER_NETWORK_ERROR, "Run: cw retry")
        with patch("cw.cli.main.CodexAdapter.run_planner", side_effect=failure):
            code, output = self.invoke("plan", "--goal", "Build a webhook handler")
        self.assertEqual(1, code)
        self.assertIn("Planner unavailable", output)
        self.assertEqual("ERROR", self.repo.state()["status"])
        self.assertEqual(0, self.repo.state()["attempt"])
        self.assertEqual("Build a webhook handler", self.repo.state()["pending_goal"])
        from cw.core.workflow import load_workflow
        self.assertEqual((), load_workflow(self.repo.root).phases)

        proposal = Planner().propose_plan(self.repo.root, project_id, "Build a webhook handler")
        with patch("cw.cli.main.CodexAdapter.run_planner", return_value=CodexResult({"phases": proposal["phases"]}, "")):
            code, output = self.invoke("retry", "--json")
        self.assertEqual(0, code)
        self.assertEqual("PROPOSED", json.loads(output)["status"])
        self.assertIsNone(self.repo.state()["pending_goal"])

    def test_explicit_reopen_backs_up_and_invalidates_dependent_gates(self):
        from cw.core.gates import create_gate
        self.repo.artifact(1); self.repo.artifact(2)
        review_1 = self.repo.approved_review(1)
        review_2 = self.repo.approved_review(2)
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], review_1)
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[1], review_2)
        code, output = self.invoke("repair", "--reopen", "01-phase-1")
        self.assertEqual(0, code)
        self.assertIn("Phase reopened", output)
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
        self.assertFalse((self.repo.root / ".cw/gates/02-phase-2.approved.json").exists())
        self.assertEqual("01-phase-1", self.repo.state()["current_phase"])
        self.assertTrue(list((self.repo.root / ".cw/backups").glob("*/gates/01-phase-1.approved.json")))

    def test_reopen_preserves_prior_review_when_attempt_number_restarts(self):
        from cw.agents.reviewer import run_review
        from cw.core.audit import audit_history
        from cw.core.gates import validate_gate
        from tests.helpers import FakeAdapter

        self.repo.artifact()
        self.repo.ready()
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result()),
        )
        original_reference = self.repo.state()["last_review"]
        original_path = self.repo.root / original_reference
        original_bytes = original_path.read_bytes()

        code, _ = self.invoke("repair", "--reopen", "01-phase-1")
        self.assertEqual(0, code)
        self.repo.ready()
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result()),
        )

        current_reference = self.repo.state()["last_review"]
        self.assertNotEqual(original_reference, current_reference)
        self.assertEqual(original_bytes, original_path.read_bytes())
        self.assertEqual(2, len(list((self.repo.root / ".cw/reviews").glob("*.json"))))
        self.assertEqual(2, audit_history(self.repo.root, self.repo.workflow, self.repo.state())["reviews"])
        validate_gate(self.repo.root, self.repo.workflow, "01-phase-1")

    def test_repair_backs_up_then_removes_corrupt_session(self):
        session = self.repo.root / ".cw/runtime/implementer-session.json"
        ready = self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        session.write_text("{}\n", encoding="utf-8")
        ready.write_text("{}\n", encoding="utf-8")
        code, _ = self.invoke("repair")
        self.assertEqual(0, code)
        self.assertFalse(session.exists())
        self.assertFalse(ready.exists())
        self.assertTrue(list((self.repo.root / ".cw/backups").glob("*/runtime/implementer-session.json")))


if __name__ == "__main__":
    unittest.main()
