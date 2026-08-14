from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.core.platform import (
    global_config_dir,
    popen_process_group_kwargs,
    process_is_alive,
    stop_process_group,
    user_bin_dir,
    user_install_root,
)
from cw.core.errors import CwError
from cw.core.locking import operation_lock
from cw.core.utils import atomic_json, atomic_write, load_json
from cw.update.installation import InstallPaths, ManagedInstallation, RuntimePointer
from scripts.install import install


class PlatformPathTests(unittest.TestCase):
    def test_windows_user_paths_use_appdata_without_admin_locations(self):
        environment = {
            "LOCALAPPDATA": r"C:\Users\Ada\AppData\Local",
            "APPDATA": r"C:\Users\Ada\AppData\Roaming",
        }
        home = Path(r"C:\Users\Ada")
        self.assertEqual(
            Path(environment["APPDATA"]) / "Queopius/CW",
            global_config_dir(environment=environment, home=home, platform="nt"),
        )
        root = user_install_root(environment=environment, home=home, platform="nt")
        self.assertEqual(Path(environment["LOCALAPPDATA"]) / "Queopius/CW", root)
        self.assertEqual(root / "bin", user_bin_dir(environment=environment, home=home, platform="nt"))

    def test_acceptance_install_overrides_must_be_absolute(self):
        with self.assertRaises(ValueError):
            user_install_root(environment={"CW_INSTALL_ROOT": "relative"})
        with self.assertRaises(ValueError):
            user_bin_dir(environment={"CW_BIN_DIR": "relative"})

    def test_xdg_override_remains_supported_on_every_platform(self):
        expected = Path("/controlled/config/cw")
        self.assertEqual(expected, global_config_dir(environment={"XDG_CONFIG_HOME": "/controlled/config"}, platform="nt"))


class RuntimePointerTests(unittest.TestCase):
    def test_windows_pointer_is_atomic_regular_file_not_symlink(self):
        with tempfile.TemporaryDirectory(prefix="cw-pointer-") as temporary:
            base = Path(temporary)
            paths = InstallPaths(base / "share", base / "bin")
            for version in ("0.5.0", "0.5.1"):
                (paths.versions / version).mkdir(parents=True)
            pointer = RuntimePointer(paths, "nt")
            pointer.activate("0.5.0")
            self.assertFalse(paths.current.is_symlink())
            self.assertEqual("0.5.0", pointer.active_version())
            pointer.activate("0.5.1")
            self.assertEqual("0.5.1", pointer.active_version())

    def test_managed_windows_runtime_is_detected_from_versioned_tree(self):
        with tempfile.TemporaryDirectory(prefix="cw-pointer-") as temporary:
            base = Path(temporary)
            paths = InstallPaths(base / "share", base / "bin")
            module = paths.versions / "0.5.1/cw/update/installation.py"
            module.parent.mkdir(parents=True)
            module.write_text("# fixture\n", encoding="utf-8")
            RuntimePointer(paths, "nt").activate("0.5.1")
            installation = ManagedInstallation(paths, module_path=module, platform="nt")
            self.assertTrue(installation.managed)
            self.assertEqual("0.5.1", installation.active_version())


class WindowsInstallerStructureTests(unittest.TestCase):
    def test_native_layout_generates_cmd_launcher_and_no_symlink(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="cw-windows-installer-") as temporary:
            base = Path(temporary)
            paths = InstallPaths(base / "Local AppData/Queopius/CW", base / "Local AppData/Queopius/CW/bin")
            with redirect_stdout(io.StringIO()):
                install(project, paths=paths, platform="nt")
                install(project, paths=paths, platform="nt")
            self.assertTrue((paths.bin / "cw.cmd").is_file())
            self.assertTrue((paths.bin / "cw-launcher.py").is_file())
            self.assertFalse(paths.current.is_symlink())
            self.assertEqual((project / "VERSION").read_text().strip(), paths.current.read_text().strip())
            command = (paths.bin / "cw.cmd").read_text(encoding="utf-8")
            self.assertIn("cw-launcher.py", command)
            self.assertNotIn("WSL", command)
            launcher = (paths.bin / "cw-launcher.py").read_text(encoding="utf-8")
            self.assertIn('os.environ["CW_INSTALL_ROOT"]', launcher)

    def test_powershell_installer_updates_only_user_path_idempotently(self):
        source = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('SetEnvironmentVariable("Path", $updated, "User")', source)
        self.assertIn("OrdinalIgnoreCase", source)
        self.assertNotIn('SetEnvironmentVariable("Path", $updated, "Machine")', source)
        self.assertNotIn("Start-Process -Verb RunAs", source)


class AtomicAndEncodingTests(unittest.TestCase):
    def test_atomic_utf8_json_survives_spaces_unicode_and_replacement(self):
        with tempfile.TemporaryDirectory(prefix="CW Acceptance ") as temporary:
            path = Path(temporary) / "Projeto São Paulo/state.json"
            atomic_json(path, {"goal": "Olá José", "status": "IN_PROGRESS"})
            first = load_json(path)
            atomic_json(path, {"goal": "Olá José", "status": "COMPLETED"})
            self.assertEqual("IN_PROGRESS", first["status"])
            self.assertEqual("COMPLETED", load_json(path)["status"])
            self.assertFalse(any(path.parent.glob("*.tmp")))

    def test_crlf_json_is_read_logically_and_new_writes_are_utf8_lf(self):
        with tempfile.TemporaryDirectory(prefix="cw-crlf-") as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b'{\r\n  "status": "READY"\r\n}\r\n')
            self.assertEqual("READY", load_json(path)["status"])
            atomic_write(path, json.dumps({"status": "READY"}, ensure_ascii=False) + "\n")
            self.assertNotIn(b"\r\n", path.read_bytes())


class NativeProcessTests(unittest.TestCase):
    def test_known_current_and_missing_process_liveness(self):
        self.assertTrue(process_is_alive(os.getpid()))
        self.assertFalse(process_is_alive(2_147_483_647))

    def test_grouped_child_streams_utf8_and_can_be_terminated(self):
        environment = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sys,time; print('São Paulo', flush=True); time.sleep(30)",
            ],
            text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment,
            **popen_process_group_kwargs(),
        )
        self.assertEqual("São Paulo", process.stdout.readline().strip())
        self.assertTrue(process_is_alive(process.pid))
        started = time.monotonic()
        stop_process_group(process, grace_seconds=0.2)
        process.communicate(timeout=1)
        self.assertIsNotNone(process.returncode)
        self.assertLess(time.monotonic() - started, 10)

    def test_external_lock_owner_blocks_concurrency_and_dead_owner_is_recovered(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="CW lock São Paulo ") as temporary:
            root = Path(temporary)
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from cw.core.locking import operation_lock\n"
                "with operation_lock(Path(sys.argv[1]), 'holder'):\n"
                " print('ready', flush=True)\n"
                " time.sleep(30)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root)], cwd=project,
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            def cleanup() -> None:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            self.addCleanup(cleanup)
            self.assertEqual("ready", process.stdout.readline().strip())
            with self.assertRaises(CwError):
                with operation_lock(root, "contender"):
                    pass
            process.kill()
            process.communicate(timeout=5)
            with operation_lock(root, "recovered"):
                self.assertTrue((root / ".cw/locks/operation.lock").is_file())


if __name__ == "__main__":
    unittest.main()
