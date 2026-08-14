from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FAKE = Path(__file__).parent / "fixtures/fake_codex/fake_codex.py"


class FakeCodexContractTests(unittest.TestCase):
    def run_fake(self, arguments, *, role, scenario="success", cwd=None, timeout=10):
        environment = {
            **os.environ,
            f"CW_{role.upper()}_ACTIVE": "1",
            "CW_FAKE_CODEX_SCENARIO": scenario,
        }
        return subprocess.run(
            [sys.executable, str(FAKE), *arguments], cwd=cwd, env=environment,
            text=True, capture_output=True, timeout=timeout, check=False,
        )

    def test_planner_writes_valid_bounded_public_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "plan.json"
            completed = self.run_fake(
                ["--cd", str(root), "exec", "--output-last-message", str(output)],
                role="planner", cwd=root,
            )
            self.assertEqual(0, completed.returncode)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(1, len(payload["phases"]))
            self.assertNotIn("reasoning", output.read_text(encoding="utf-8").lower())

    def test_planner_failure_and_malformed_output_are_detectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); output = root / "plan.json"
            failed = self.run_fake(
                ["--cd", str(root), "exec", "--output-last-message", str(output)],
                role="planner", scenario="planner_failure", cwd=root,
            )
            self.assertNotEqual(0, failed.returncode)
            malformed = self.run_fake(
                ["--cd", str(root), "exec", "--output-last-message", str(output)],
                role="planner", scenario="malformed_output", cwd=root,
            )
            self.assertEqual(0, malformed.returncode)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(output.read_text(encoding="utf-8"))

    def test_unmanaged_invocation_fails_closed(self):
        completed = subprocess.run(
            [sys.executable, str(FAKE)], text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(0, completed.returncode)


if __name__ == "__main__":
    unittest.main()
