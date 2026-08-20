from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cw import __version__
from cw.cli.main import main


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_VERSION = (ROOT / "plugins/cw/VERSION").read_text(encoding="utf-8").strip()


class CliVersionCompatibilityTests(unittest.TestCase):
    def run_cli(self, base: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        cwd = base / "arbitrary-directory"
        home = base / "isolated-home"
        cwd.mkdir(exist_ok=True)
        home.mkdir(exist_ok=True)
        environment = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "CW_INSTALL_ROOT": str(base / "managed"),
            "CW_BIN_DIR": str(base / "bin"),
            "CW_NO_UPDATE_CHECK": "1",
            "PYTHONPATH": str(ROOT),
        }
        return subprocess.run(
            [sys.executable, "-m", "cw.cli.main", *arguments],
            cwd=cwd,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    @staticmethod
    def snapshot(path: Path) -> list[tuple[str, bool, bytes | None]]:
        return [
            (item.relative_to(path).as_posix(), item.is_dir(), None if item.is_dir() else item.read_bytes())
            for item in sorted(path.rglob("*"))
        ]

    def test_version_flag_exits_before_dispatch_with_exact_core_version(self) -> None:
        output = StringIO()
        with patch("cw.cli.main.run") as dispatch, redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                main(("--version",))
        self.assertEqual(0, caught.exception.code)
        self.assertEqual(f"CW {__version__}\n", output.getvalue())
        dispatch.assert_not_called()

    def test_both_version_surfaces_work_outside_git_without_project_or_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-version-") as temporary:
            base = Path(temporary)
            (base / "arbitrary-directory").mkdir()
            (base / "isolated-home").mkdir()
            before = self.snapshot(base)
            version_flag = self.run_cli(base, "--version")
            after_flag = self.snapshot(base)
            version_command = self.run_cli(base, "version")

        self.assertEqual(0, version_flag.returncode, version_flag.stderr)
        self.assertEqual(0, version_command.returncode, version_command.stderr)
        self.assertEqual(f"CW {__version__}", version_flag.stdout.strip())
        self.assertEqual(f"CW {__version__}", version_command.stdout.splitlines()[0])
        self.assertEqual(before, after_flag)
        self.assertNotIn(f"CW {PLUGIN_VERSION}", version_flag.stdout)
        self.assertNotIn(f"CW {PLUGIN_VERSION}", version_command.stdout)

    def test_short_and_long_help_remain_coherent_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-help-") as temporary:
            short = self.run_cli(Path(temporary), "-h")
            long = self.run_cli(Path(temporary), "--help")
        self.assertEqual(0, short.returncode, short.stderr)
        self.assertEqual(0, long.returncode, long.stderr)
        self.assertEqual(short.stdout, long.stdout)
        self.assertIn("version       Show version", short.stdout)


if __name__ == "__main__":
    unittest.main()
