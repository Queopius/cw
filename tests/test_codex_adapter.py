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

    def test_implementer_nonzero_exit_is_classified(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.call", return_value=17
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_implementer(Path(temporary), "implement")
        self.assertEqual(ErrorCode.IMPLEMENTER_PROCESS_ERROR, raised.exception.code)
        self.assertIn("17", raised.exception.details or "")

    def test_reviewer_is_read_only_ephemeral_and_hooks_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            def fake_run(command, **kwargs):
                self.assertIn("read-only", command)
                self.assertIn("--ephemeral", command)
                self.assertEqual(command[command.index("--disable") + 1], "hooks")
                self.assertIn('web_search="disabled"', command)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"decision": "APPROVE"}))
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch("cw.adapters.codex.subprocess.run", side_effect=fake_run):
                result = CodexAdapter().run_reviewer(root, "review", schema, 10)
            self.assertEqual("APPROVE", result.payload["decision"])


if __name__ == "__main__":
    unittest.main()
