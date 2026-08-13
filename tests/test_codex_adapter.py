from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.adapters.codex import CodexAdapter
from cw.core.errors import CwError, ErrorCode


class CodexAdapterTests(unittest.TestCase):
    def test_implementer_denies_network_and_web_search_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.call", return_value=0
            ) as call:
                CodexAdapter().run_implementer(root, "implement")
            command = call.call_args.args[0]
            self.assertIn("--strict-config", command)
            self.assertIn("sandbox_workspace_write.network_access=false", command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn("workspace-write", command)

    def test_implementer_policy_can_allow_sandbox_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.call", return_value=0
            ) as call:
                CodexAdapter().run_implementer(root, "implement", allow_network=True)
            command = call.call_args.args[0]
            self.assertIn("sandbox_workspace_write.network_access=true", command)
            self.assertNotIn('web_search="disabled"', command)

    def test_implementer_exports_session_to_hook_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.call", return_value=0
            ) as call:
                CodexAdapter().run_implementer(root, "implement", session_id="a" * 32)
        self.assertEqual("1", call.call_args.kwargs["env"]["CW_IMPLEMENTER_ACTIVE"])
        self.assertEqual("a" * 32, call.call_args.kwargs["env"]["CW_IMPLEMENTER_SESSION"])

    def test_implementer_nonzero_exit_is_classified(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.call", return_value=17
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_implementer(Path(temporary), "implement")
        self.assertEqual(ErrorCode.IMPLEMENTER_PROCESS_ERROR, raised.exception.code)
        self.assertIn("17", raised.exception.details or "")

    def test_batch_interrupt_terminates_child_before_propagating(self):
        class InterruptedProcess:
            def __init__(self):
                self.terminated = False

            def wait(self, timeout=None):
                if not self.terminated:
                    raise KeyboardInterrupt
                return 130

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.terminated = True

        process = InterruptedProcess()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.adapters.codex.shutil.which", return_value="/usr/bin/codex",
        ), patch("cw.adapters.codex.IntegrationManager.configured", return_value=()), patch(
            "cw.adapters.codex.subprocess.Popen", return_value=process,
        ), self.assertRaises(KeyboardInterrupt):
            CodexAdapter().run_implementer(Path(temporary), "implement", timeout=60)
        self.assertTrue(process.terminated)

    def test_reviewer_is_read_only_ephemeral_and_hooks_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            def fake_run(command, **kwargs):
                self.assertIn("read-only", command)
                self.assertIn("--ephemeral", command)
                self.assertEqual(command[command.index("--disable") + 1], "hooks")
                self.assertIn('web_search="disabled"', command)
                self.assertIn("project_doc_max_bytes=0", command)
                self.assertEqual("review", command[-1])
                self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"decision": "APPROVE"}))
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch("cw.adapters.codex.subprocess.run", side_effect=fake_run):
                result = CodexAdapter().run_reviewer(root, "review", schema, 10)
            self.assertEqual("APPROVE", result.payload["decision"])

    def test_planner_is_structured_read_only_ephemeral_and_rules_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            def fake_run(command, **kwargs):
                self.assertIn("read-only", command)
                self.assertIn("--ephemeral", command)
                self.assertIn("--ignore-rules", command)
                self.assertEqual(command[command.index("--disable") + 1], "hooks")
                self.assertIn('web_search="disabled"', command)
                self.assertIn("project_doc_max_bytes=0", command)
                self.assertEqual("plan", command[-1])
                self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"phases": []}))
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=fake_run
            ):
                result = CodexAdapter().run_planner(root, "plan", schema, 10)
            self.assertEqual([], result.payload["phases"])

    def test_planner_network_failure_is_classified_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            failure = subprocess.CompletedProcess([], 1, "", "WebSocket connection failed")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", return_value=failure
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_planner(root, "plan", schema, 10)
            self.assertEqual(ErrorCode.PLANNER_TRANSPORT_ERROR, raised.exception.code)

    def test_planner_timeout_is_classified_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            timeout = subprocess.TimeoutExpired(["codex"], 10)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=timeout
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_planner(root, "plan", schema, 10)
            self.assertEqual(ErrorCode.PLAN_TIMEOUT, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
