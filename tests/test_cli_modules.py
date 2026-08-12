from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from cw.cli.commands.execution import current_phase
from cw.cli.commands.read import command_version
from cw.cli.parser import normalized_argv, parse_args
from cw.cli.runner import run
from cw.core.errors import CwError, ErrorCode
from cw.ui.console import Console
from tests.helpers import TempRepo


class CliParserTests(unittest.TestCase):
    def test_default_and_public_help_are_normalized(self):
        self.assertEqual(["start"], normalized_argv([]))
        self.assertEqual(["help"], normalized_argv(["--help"]))
        self.assertEqual(["help"], normalized_argv(["-h"]))

    def test_plan_arguments_are_parsed_independently(self):
        args = parse_args(["plan", "rebuild", "--goal", "Ship subscriptions", "--json"])
        self.assertEqual("plan", args.command)
        self.assertEqual("rebuild", args.action)
        self.assertEqual("Ship subscriptions", args.goal)
        self.assertTrue(args.json)


class CliRunnerTests(unittest.TestCase):
    def invoke(self, args, commands):
        recorded = []
        output = io.StringIO()

        def recorder(error, **metadata):
            recorded.append((error, metadata))

        with redirect_stdout(output):
            code = run(args, commands=commands, record_error=recorder)
        return code, output.getvalue(), recorded

    def test_help_json_comes_from_injected_registry(self):
        code, output, recorded = self.invoke(parse_args(["help", "--json"]), {"status": lambda *_: 0})
        self.assertEqual(0, code)
        self.assertEqual(["status", "help"], json.loads(output)["commands"])
        self.assertEqual([], recorded)

    def test_classified_error_is_recorded_and_returned_as_json(self):
        failure = CwError("bad configuration", ErrorCode.USAGE_ERROR, "Run: cw help", exit_code=2)

        def command(*_args):
            raise failure

        code, output, recorded = self.invoke(parse_args(["status", "--json"]), {"status": command})
        self.assertEqual(2, code)
        self.assertEqual("USAGE_ERROR", json.loads(output)["error"]["code"])
        self.assertIs(failure, recorded[0][0])
        self.assertEqual("status", recorded[0][1]["source"])

    def test_unexpected_error_is_redacted_but_traceback_is_recorded(self):
        def command(*_args):
            raise ValueError("internal detail")

        code, output, recorded = self.invoke(parse_args(["status"]), {"status": command})
        self.assertEqual(1, code)
        self.assertIn("CW encountered an internal error", output)
        self.assertNotIn("internal detail", output)
        self.assertEqual(ErrorCode.INTERNAL_ERROR, recorded[0][0].code)
        self.assertIn("ValueError: internal detail", recorded[0][1]["traceback_text"])

    def test_keyboard_interrupt_returns_shell_convention(self):
        def command(*_args):
            raise KeyboardInterrupt

        code, output, recorded = self.invoke(parse_args(["status"]), {"status": command})
        self.assertEqual(130, code)
        self.assertEqual("", output)
        self.assertEqual([], recorded)


class CliCommandModuleTests(unittest.TestCase):
    def test_current_phase_resolves_without_cli_globals(self):
        repository = TempRepo()
        try:
            self.assertEqual("01-phase-1", current_phase(repository.workflow, repository.state()).id)
        finally:
            repository.close()

    def test_version_command_emits_stable_json_directly(self):
        args = parse_args(["version", "--json"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = command_version(args, Console())
        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("CW by Queopius", payload["brand"])
        self.assertEqual("Codex Workflow", payload["product"])


if __name__ == "__main__":
    unittest.main()
