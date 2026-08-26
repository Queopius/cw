from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_acceptance import (
    AcceptanceFailure,
    _capture_cw_diagnostic,
    _environment,
    _install_fake_codex,
    _result,
    _run,
    _sanitize_detail,
    _surface,
    _validate_report,
    _write_diagnostic,
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
            r"C:\\Users\\Ada\\AppData\\Temp\\fixture Authorization: Bearer private-token",
        )
        self.assertNotIn("users", detail.lower())
        self.assertNotIn("authorization", detail.lower())
        self.assertNotIn("bearer", detail.lower())
        self.assertNotIn("private-token", detail)
        self.assertIn("[REDACTED CREDENTIAL]", detail)

    def test_allowlisted_surface_never_contains_private_arguments(self):
        self.assertEqual("cw plan approve", _surface([r"C:\private\cw.exe", "plan", "approve", "--secret=x"]))
        self.assertEqual("cw run", _surface(["cw"]))
        self.assertEqual("python unittest", _surface(["python", "-m", "unittest", "private.module"]))

    def test_failure_diagnostic_redacts_paths_secrets_and_email(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "Café São Paulo"
            base.mkdir()
            failure = AcceptanceFailure(
                r"C:\Users\Ada\worktree\秘密 token=abc Bearer xyz ada@example.com",
                stage="cw run", surface="cw run", exit_code=1,
            )
            output = base / "artifacts/compatibility-report.json"
            _write_diagnostic(output, failure, base=base, source_commit="deadbeef")
            diagnostic = json.loads((output.parent / "compatibility-diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual("cw.acceptance-diagnostic.v1", diagnostic["schema"])
        self.assertEqual("cw run", diagnostic["stage"])
        serialized = json.dumps(diagnostic, ensure_ascii=False).lower()
        for forbidden in ("users", "ada", "token=abc", "bearer xyz", "example.com", "秘密"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual("AcceptanceFailure", diagnostic["primary_error"]["exception_type"])

    def test_successful_report_does_not_require_a_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "compatibility-report.json"
            self.assertFalse(artifact.with_name("compatibility-diagnostic.json").exists())

    def test_cw_failure_capture_uses_only_allowlisted_state_and_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / ".cw/logs"; logs.mkdir(parents=True)
            (root / ".cw/runtime/reviewer").mkdir(parents=True)
            (root / ".cw/state.json").write_text(
                json.dumps({"status": "ERROR", "current_phase": "01-phase", "attempt": 0, "revision_attempt": 0, "secret": "no"})
            )
            (logs / "last-error.json").write_text(json.dumps({"code": "REVIEWER_INFRASTRUCTURE_ERROR", "message": r"C:\\Users\\Ada\\secret", "hint": "Run: cw retry", "prompt": "ignore"}))
            failure = AcceptanceFailure("cw run exited 1", stage="cw run", surface="cw run", exit_code=1, command=["cw"], cwd=root)
            completed = __import__("subprocess").CompletedProcess(["cw", "error"], 0, '{"error":{"code":"REVIEWER_INFRASTRUCTURE_ERROR","message":"failed"}}', "")
            with patch("scripts.run_acceptance.subprocess.run", return_value=completed):
                cw_error, state, runtime = _capture_cw_diagnostic(failure, private_roots=(root,))
        self.assertEqual("REVIEWER_INFRASTRUCTURE_ERROR", cw_error["code"])
        self.assertEqual("ERROR", state["status"])
        self.assertNotIn("secret", state)
        self.assertEqual([".cw/runtime/reviewer"], runtime["entries"])
        self.assertNotIn("users", json.dumps(state).lower())

    def test_cw_error_failure_is_recorded_without_overwriting_primary_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / ".cw").mkdir()
            failure = AcceptanceFailure("cw run exited 1", stage="cw run", surface="cw run", exit_code=1, command=["cw"], cwd=root)
            completed = __import__("subprocess").CompletedProcess(["cw", "error"], 7, "", "")
            with patch("scripts.run_acceptance.subprocess.run", return_value=completed):
                cw_error, _state, _runtime = _capture_cw_diagnostic(failure, private_roots=(root,))
        self.assertEqual("cw error", cw_error["surface"])
        self.assertEqual(7, cw_error["exit_code"])

    def test_capture_before_init_and_corrupt_error_log_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failure = AcceptanceFailure("cw init exited 1", stage="cw init", surface="cw init", command=["cw"], cwd=root)
            self.assertEqual(({}, {}, {}), _capture_cw_diagnostic(failure, private_roots=(root,)))
            (root / ".cw/logs").mkdir(parents=True)
            (root / ".cw/logs/last-error.json").write_text("{not json", encoding="utf-8")
            completed = subprocess.CompletedProcess(["cw", "error"], 0, "{not json", "")
            with patch("scripts.run_acceptance.subprocess.run", return_value=completed):
                cw_error, state, runtime = _capture_cw_diagnostic(failure, private_roots=(root,))
        self.assertEqual({"surface": "cw error", "exit_code": 0}, cw_error)
        self.assertEqual({}, state)
        self.assertEqual({"entries": []}, runtime)

    def test_timeout_is_preserved_as_sanitized_stage_metadata(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "scripts.run_acceptance.subprocess.run", side_effect=subprocess.TimeoutExpired(["cw"], 17)
        ), self.assertRaises(AcceptanceFailure) as raised:
            _run(["cw", "retry"], cwd=Path(temporary), environment={}, timeout=17)
        self.assertTrue(raised.exception.timed_out)
        self.assertEqual("cw retry", raised.exception.stage)
        self.assertIsNone(raised.exception.exit_code)


if __name__ == "__main__":
    unittest.main()
