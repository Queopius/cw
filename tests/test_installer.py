from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.install import _launcher_script, copy_runtime, install
from cw.update.installation import InstallPaths
from cw.core.build import git_build


class InstallerTests(unittest.TestCase):
    def test_launcher_prefers_active_runtime_and_its_dependency_directory(self):
        script = _launcher_script(Path("/managed/cw"))
        self.assertIn('dependencies = runtime / "python"', script)
        self.assertLess(
            script.index('sys.path.insert(0, str(dependencies))'),
            script.index('sys.path.insert(0, str(runtime))'),
        )

    def test_source_installer_can_provision_remote_without_mutating_previous_runtime(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="cw-remote-installer-") as temporary:
            base = Path(temporary)
            paths = InstallPaths(base / "share/cw", base / "bin")
            previous = paths.versions / "0.18.2"
            previous.mkdir(parents=True)
            (previous / "entrypoint.py").write_text("print('{}')\n", encoding="utf-8")
            paths.share.mkdir(parents=True, exist_ok=True)
            from cw.update.installation import RuntimePointer
            RuntimePointer(paths).activate("0.18.2")

            def fake_remote(directory: Path) -> None:
                target = directory / "python"
                target.mkdir()
                for module in ("httpx", "jwt", "cryptography", "mcp", "uvicorn"):
                    (target / f"{module}.py").write_text("# fixture\n", encoding="utf-8")

            install(project, paths=paths, with_remote=True, remote_installer=fake_remote)
            current = paths.current.resolve()
            self.assertEqual("0.18.3", current.name)
            self.assertTrue((current / "python/httpx.py").is_file())
            self.assertTrue(previous.is_dir())
    def test_runtime_contains_source_build_fingerprint(self):
        project = Path(__file__).resolve().parents[1]
        expected = git_build(project)
        with tempfile.TemporaryDirectory(prefix="cw-build-") as temporary:
            destination = Path(temporary) / "runtime"
            destination.mkdir()
            copy_runtime(project, destination)
            metadata = json.loads((destination / "BUILD.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, metadata["commit"])
        self.assertEqual("source-install", metadata["source"])

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
            share = home / ".local/share/cw"
            current = share / "current"
            self.assertTrue(current.is_symlink())
            self.assertTrue((current / "LICENSE").is_file())
            self.assertIn("Copyright 2026 Fantomid LLC", (current / "NOTICE").read_text())
            self.assertIn("CW by Queopius", (current / "NOTICE").read_text())
            self.assertEqual(1, (home / ".zshrc").read_text().count('export PATH="$HOME/.local/bin:$PATH"'))
            self.assertEqual(1, (home / ".profile").read_text().count('export PATH="$HOME/.local/bin:$PATH"'))
            shutil.rmtree(source)
            completed = subprocess.run([str(launcher), "version", "--json"], env=environment, text=True, capture_output=True, check=True)
            expected = (project / "VERSION").read_text(encoding="utf-8").strip()
            self.assertEqual(expected, json.loads(completed.stdout)["version"])
            self.assertTrue((share / "versions" / expected).is_dir())
            modules = subprocess.run([
                "python3", "-c",
                "import cw.cli.commands.config, cw.cli.commands.execution, cw.cli.commands.lifecycle, cw.cli.commands.read, cw.cli.parser, cw.cli.runner",
            ], cwd=home, env={**environment, "PYTHONPATH": str(current)}, text=True, capture_output=True)
            self.assertEqual(0, modules.returncode, modules.stderr)


if __name__ == "__main__":
    unittest.main()
