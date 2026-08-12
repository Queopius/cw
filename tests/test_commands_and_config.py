from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cw.checks.deterministic import validate_phase
from cw.cli.main import _context
from cw.core.commands import command_arguments
from cw.core.config import load_policy
from cw.core.errors import CwError, ErrorCode
from cw.core.models import RequiredCommand
from cw.planning.planner import Planner
from tests.helpers import TempRepo


class CommandSecurityTests(unittest.TestCase):
    def test_quoted_arguments_are_parsed_without_a_shell(self):
        self.assertEqual(["python3", "-c", "print('safe value')"], command_arguments('python3 -c "print(\'safe value\')"'))

    def test_shell_control_syntax_is_rejected(self):
        for command in (
            "python3 -m unittest && touch escaped",
            "python3 -m unittest | tee output",
            "python3 -m unittest > output",
            "python3 -c `id`",
            "python3 -c $(id)",
        ):
            with self.subTest(command=command), self.assertRaises(CwError) as raised:
                command_arguments(command)
            self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, raised.exception.code)

    def test_shell_interpreters_are_rejected(self):
        for command in ("sh -c true", "/bin/bash -lc true", "pwsh -Command Get-ChildItem"):
            with self.subTest(command=command), self.assertRaises(CwError):
                command_arguments(command)

    def test_validation_does_not_execute_redirection(self):
        repo = TempRepo(phases=1)
        try:
            repo.artifact()
            repo.ready()
            command = RequiredCommand("printf compromised > escaped.txt")
            phase = replace(repo.workflow.phases[0], required_commands=(command,))
            workflow = replace(repo.workflow, phases=(phase,))
            result = validate_phase(repo.root, workflow, phase)
            self.assertFalse(result.passed)
            self.assertFalse((repo.root / "escaped.txt").exists())
            self.assertIn("unsupported shell syntax", result.errors[0])
        finally:
            repo.close()

    def test_missing_executable_is_a_compact_validation_failure(self):
        repo = TempRepo(phases=1)
        try:
            repo.artifact()
            repo.ready()
            command = RequiredCommand("cw-command-that-does-not-exist")
            phase = replace(repo.workflow.phases[0], required_commands=(command,))
            workflow = replace(repo.workflow, phases=(phase,))
            result = validate_phase(repo.root, workflow, phase)
            self.assertFalse(result.passed)
            self.assertIn("could not start", result.errors[0])
        finally:
            repo.close()

    def test_safe_command_executes_directly(self):
        repo = TempRepo(phases=1)
        try:
            repo.artifact()
            command_text = f"{shlex.quote(sys.executable)} -c pass"
            repo.ready(checks=[{"command": command_text, "exit_code": 0}])
            command = RequiredCommand(command_text)
            phase = replace(repo.workflow.phases[0], required_commands=(command,))
            workflow = replace(repo.workflow, phases=(phase,))
            self.assertTrue(validate_phase(repo.root, workflow, phase).passed)
        finally:
            repo.close()


class EffectivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(phases=1)
        self.initial_config = (self.repo.root / ".cw" / "config.toml").read_text(encoding="utf-8")
        # New project files contain comments only; reproduce that behavior for
        # this fixture, which predates the initializer change in this test run.
        (self.repo.root / ".cw" / "config.toml").write_text("# project overrides\n", encoding="utf-8")
        self.xdg = tempfile.TemporaryDirectory(prefix="cw-config-")
        self.global_config = Path(self.xdg.name) / "cw" / "config.toml"
        self.global_config.parent.mkdir(parents=True)

    def tearDown(self):
        self.xdg.cleanup()
        self.repo.close()

    def test_new_project_does_not_mask_global_preferences(self):
        self.assertNotIn("\nmax_review_attempts =", self.initial_config)
        self.assertNotIn("\ncommand_timeout =", self.initial_config)
        self.assertNotIn("\nreview_timeout =", self.initial_config)

    def test_global_policy_overrides_workflow_defaults(self):
        self.global_config.write_text(
            "max_review_attempts = 7\ncommand_timeout = 41\nreview_timeout = 43\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg.name}):
            _, _, workflow = _context(self.repo.root)
        self.assertEqual(7, workflow.max_review_attempts)
        self.assertEqual(41, workflow.command_timeout)
        self.assertEqual(43, workflow.review_timeout)

    def test_project_policy_overrides_global_policy(self):
        self.global_config.write_text("max_review_attempts = 7\n", encoding="utf-8")
        (self.repo.root / ".cw" / "config.toml").write_text("max_review_attempts = 2\n", encoding="utf-8")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg.name}):
            _, _, workflow = _context(self.repo.root)
        self.assertEqual(2, workflow.max_review_attempts)

    def test_agent_policy_values_are_applied_to_workflow(self):
        (self.repo.root / ".cw" / "config.toml").write_text(
            'allow_network = true\nhuman_gate_categories = ["authentication-security"]\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg.name}):
            _, _, workflow = _context(self.repo.root)
        self.assertTrue(workflow.allow_network)
        self.assertEqual(("authentication-security",), workflow.human_gate_categories)

    def test_configured_human_gate_categories_drive_planning(self):
        authentication = Planner(("authentication-security",)).propose_plan(
            self.repo.root, "sample-app", "Strengthen authorization and access control"
        )
        payments_only = Planner(("payments",)).propose_plan(
            self.repo.root, "sample-app", "Strengthen authorization and access control"
        )
        disabled = Planner(()).propose_plan(
            self.repo.root, "sample-app", "Implement subscription billing"
        )
        self.assertTrue(authentication["phases"][1]["requires_human_approval"])
        self.assertFalse(payments_only["phases"][1]["requires_human_approval"])
        self.assertFalse(disabled["phases"][1]["requires_human_approval"])

    def test_invalid_policy_fails_as_configuration_error(self):
        (self.repo.root / ".cw" / "config.toml").write_text("review_timeout = 0\n", encoding="utf-8")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg.name}):
            with self.assertRaises(CwError) as raised:
                load_policy(self.repo.root, workflow=self.repo.workflow)
        self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)
        self.assertEqual(2, raised.exception.exit_code)

    def test_unknown_policy_key_fails_closed(self):
        (self.repo.root / ".cw" / "config.toml").write_text("secret_mode = true\n", encoding="utf-8")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg.name}):
            with self.assertRaises(CwError) as raised:
                load_policy(self.repo.root, workflow=self.repo.workflow)
        self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)

    def test_malformed_toml_fails_closed(self):
        (self.repo.root / ".cw" / "config.toml").write_text("this is not toml\n", encoding="utf-8")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg.name}):
            with self.assertRaises(CwError) as raised:
                load_policy(self.repo.root, workflow=self.repo.workflow)
        self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
