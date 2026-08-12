from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ReleaseDemoTests(unittest.TestCase):
    def test_installed_two_repository_isolation_demo(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="cw-release-demo-") as temporary:
            root = Path(temporary)
            home = root / "home"; home.mkdir()
            environment = {**os.environ, "HOME": str(home)}
            subprocess.run([str(project / "install.sh")], env=environment, check=True, capture_output=True, text=True)
            completed = subprocess.run([
                sys.executable, str(project / "scripts/demo_isolation.py"),
                "--home", str(home), "--base", str(root / "repositories"),
            ], env=environment, check=True, capture_output=True, text=True)
            payload = json.loads(completed.stdout)
            self.assertEqual("PASS", payload["result"])
            self.assertTrue(all(payload["checks"].values()))
            self.assertNotEqual(payload["plans"]["a"], payload["plans"]["b"])


if __name__ == "__main__":
    unittest.main()
