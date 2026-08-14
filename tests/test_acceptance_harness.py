from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_acceptance import (
    AcceptanceFailure,
    _environment,
    _install_fake_codex,
    _result,
    _sanitize_detail,
    _validate_report,
)


class AcceptanceHarnessTests(unittest.TestCase):
    def test_report_status_vocabulary_fails_closed(self):
        self.assertEqual("PASS", _result("PASS")["status"])
        with self.assertRaises(ValueError):
            _result("MAYBE")

    def test_report_validator_rejects_malformed_or_secret_evidence(self):
        with self.assertRaises(AcceptanceFailure):
            _validate_report({"schema_version": 1})
        report = {
            "schema_version": 1, "cw_version": "0.5.1", "source_commit": "abc", "generated_at": "now",
            "os": "Linux", "os_version": "test", "architecture": "x86_64", "python_version": "3.13",
            "install_method": "wheel", "tests": {"smoke": {"status": "PASS", "detail": "Bearer secret"}},
            "delegated": {"windows": "CI_REQUIRED", "macos": "CI_REQUIRED", "real_codex": "NOT_CONFIGURED"},
        }
        with self.assertRaises(AcceptanceFailure):
            _validate_report(report)

    def test_external_fake_launcher_is_created_for_current_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            launcher = _install_fake_codex(Path(temporary))
            self.assertTrue(launcher.is_file())
            if os.name != "nt":
                self.assertTrue(os.access(launcher, os.X_OK))

    def test_acceptance_environment_isolated_and_auth_free(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CODEX_HOME": "/private/auth", "PYTHONPATH": "/source/leak", "PATH": os.environ.get("PATH", "")},
            clear=False,
        ):
            base = Path(temporary)
            runtime = base / "runtime"
            (runtime / ("Scripts" if os.name == "nt" else "bin")).mkdir(parents=True)
            fake = base / "fake"
            fake.mkdir()
            environment = _environment(base, runtime, fake)
            self.assertNotIn("CODEX_HOME", environment)
            self.assertNotIn("PYTHONPATH", environment)
            self.assertEqual("1", environment["PYTHONUTF8"])
            self.assertIn(str(fake), environment["PATH"])

    def test_failure_detail_redacts_secrets_and_private_windows_paths(self):
        detail = _sanitize_detail(
            r"C:\Users\Ada\AppData\Temp\fixture Authorization: Bearer private-token",
        )
        self.assertNotIn(r"C:\Users", detail)
        self.assertNotIn("private-token", detail)
        self.assertIn("[REDACTED]", detail)


if __name__ == "__main__":
    unittest.main()
