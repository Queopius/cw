from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.adapters.codex import CodexAdapter


class CodexAdapterTests(unittest.TestCase):
    def test_reviewer_is_read_only_ephemeral_and_hooks_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            def fake_run(command, **kwargs):
                self.assertIn("read-only", command)
                self.assertIn("--ephemeral", command)
                self.assertEqual(command[command.index("--disable") + 1], "hooks")
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"decision": "APPROVE"}))
                return __import__("subprocess").CompletedProcess(command, 0, "", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch("cw.adapters.codex.subprocess.run", side_effect=fake_run):
                result = CodexAdapter().run_reviewer(root, "review", schema, 10)
            self.assertEqual("APPROVE", result.payload["decision"])


if __name__ == "__main__":
    unittest.main()
