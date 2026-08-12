from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    def test_idempotent_source_independent_install(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="cw-installer-") as temporary:
            base = Path(temporary)
            source = base / "source"
            shutil.copytree(project, source, ignore=shutil.ignore_patterns(".git", "__pycache__", ".cw", ".codex"))
            home = base / "home"; home.mkdir()
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment.pop("XDG_DATA_HOME", None)
            command = [str(source / "install.sh")]
            subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
            subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
            launcher = home / ".local/bin/cw"
            self.assertTrue(launcher.is_file())
            self.assertFalse(launcher.is_symlink())
            self.assertTrue((home / ".local/share/cw/LICENSE").is_file())
            self.assertIn("Copyright 2026 Fantomid LLC", (home / ".local/share/cw/NOTICE").read_text())
            self.assertIn("CW by Queopius", (home / ".local/share/cw/NOTICE").read_text())
            self.assertEqual(1, (home / ".zshrc").read_text().count('export PATH="$HOME/.local/bin:$PATH"'))
            self.assertEqual(1, (home / ".profile").read_text().count('export PATH="$HOME/.local/bin:$PATH"'))
            shutil.rmtree(source)
            completed = subprocess.run([str(launcher), "version", "--json"], env=environment, text=True, capture_output=True, check=True)
            self.assertEqual("0.1.0", json.loads(completed.stdout)["version"])


if __name__ == "__main__":
    unittest.main()
