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
    _canonical_root,
    _capture_cw_diagnostic,
    _environment,
    _install_fake_codex,
    _result,
    _run,
    _safe_regular_text,
    _text_traceback_frames,
    _validate_diagnostic,
    _validate_report,
    _write_diagnostic,
)


class AcceptanceHarnessTests(unittest.TestCase):
    canaries = ("GOAL_PRIVATE_CANARY", "GOAL_«quoted»\n秘密", "TOKEN_PRIVATE_CANARY", "C:/Users/RunnerPrivate/checkout", r"\\server\private\fixture", "/home/runner-private/project", "runner-private@example.invalid", "STDERR_PRIVATE_CANARY")

    def _failure(self, root: Path, correlation: str = "corr_123") -> AcceptanceFailure:
        return AcceptanceFailure("raw failure", stage="plan.create", executable="cw", command_name="plan", exit_code=1, executable_path="cw", cwd=root, environment={"PRIVATE_ENV": self.canaries[2]}, envelope_code="INTERNAL_ERROR", envelope_correlation=correlation, error_fingerprint="before")

    def _record(self, correlation: str) -> dict[str, object]:
        return {"correlation_id": correlation, "code": "INTERNAL_ERROR", "message": self.canaries[1], "source": "plan", "exception_type": "ValueError", "traceback": [{"path": r"C:\host\site-packages\cw\cli\runner.py", "function": "run", "line": 73, "exception_type": "ValueError", "message": self.canaries[2]}]}

    def _write_record(self, root: Path, record: dict[str, object]) -> None:
        (root / ".cw/logs").mkdir(parents=True)
        (root / ".cw/logs/last-error.json").write_text(json.dumps(record), encoding="utf-8")

    def _cw_error(self, correlation: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["cw", "error"], 0, json.dumps({"data": {"correlation_id": correlation}}), "")

    def test_report_and_environment_contracts_remain(self):
        self.assertEqual("PASS", _result("PASS")["status"])
        with self.assertRaises(ValueError): _result("MAYBE")
        with self.assertRaises(AcceptanceFailure): _validate_report({"schema_version": 1})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime = root / "runtime"; (runtime / ("Scripts" if os.name == "nt" else "bin")).mkdir(parents=True)
            environment = _environment(root, runtime, root)
            self.assertNotIn("CODEX_HOME", environment)
            self.assertTrue(_install_fake_codex(root).is_file())

    def test_declared_stage_never_uses_goal_or_process_output(self):
        completed = subprocess.CompletedProcess(["cw"], 1, self.canaries[0], self.canaries[-1])
        with tempfile.TemporaryDirectory() as temporary, patch("scripts.run_acceptance.subprocess.run", return_value=completed), self.assertRaises(AcceptanceFailure) as raised:
            _run(["cw", "plan", "--goal", self.canaries[0]], cwd=Path(temporary), environment={}, diagnostic_stage="plan.create", diagnostic_executable="cw", diagnostic_command="plan")
        self.assertEqual("plan.create", raised.exception.stage)
        self.assertEqual("plan", raised.exception.command_name)
        self.assertNotIn(self.canaries[0], str(raised.exception))

    def test_correlated_last_error_captures_only_relative_cw_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); correlation = "corr_123"; self._write_record(root, self._record(correlation))
            with patch("scripts.run_acceptance.subprocess.run", return_value=self._cw_error(correlation)):
                captured = _capture_cw_diagnostic(self._failure(root))
        self.assertEqual("captured", captured["diagnostic_status"])
        self.assertEqual("last_error", captured["diagnostic_source"])
        self.assertEqual("cw/cli/runner.py", captured["module"])
        self.assertEqual("ValueError", captured["exception_type"])
        self.assertNotIn(correlation, json.dumps(captured))

    def test_jsonl_requires_exact_correlation_and_rejects_old_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); logs = root / ".cw/logs"; logs.mkdir(parents=True)
            logs.joinpath("errors.jsonl").write_text(json.dumps(self._record("old_corr")) + "\n" + json.dumps(self._record("corr_456")) + "\n", encoding="utf-8")
            with patch("scripts.run_acceptance.subprocess.run", return_value=self._cw_error("corr_456")):
                self.assertEqual("unavailable", _capture_cw_diagnostic(self._failure(root, "corr_456"))["diagnostic_status"])
            with patch("scripts.run_acceptance.subprocess.run", return_value=self._cw_error("different_corr")):
                self.assertEqual("unavailable", _capture_cw_diagnostic(self._failure(root, "different_corr"))["diagnostic_status"])

    def test_diagnostic_record_requires_the_same_command_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); correlation = "corr_123"; record = self._record(correlation); record["source"] = "status"; self._write_record(root, record)
            with patch("scripts.run_acceptance.subprocess.run", return_value=self._cw_error(correlation)):
                captured = _capture_cw_diagnostic(self._failure(root, correlation))
        self.assertEqual("unavailable", captured["diagnostic_status"])
        self.assertEqual("correlation_mismatch", captured["binding_failure_reason"])

    def test_corrupt_oversized_symlink_and_hardlink_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); logs = root / ".cw/logs"; logs.mkdir(parents=True); target = root / "target"; target.write_text("{}", encoding="utf-8"); log = logs / "last-error.json"
            try:
                log.symlink_to(target); self.assertIsNone(_safe_regular_text(log)); log.unlink()
            except OSError:
                self.assertEqual("nt", os.name, "only Windows may prohibit developer symlink fixtures")
            os.link(target, log); self.assertIsNone(_safe_regular_text(log)); log.unlink()
            log.write_bytes(b"x" * (64 * 1024 + 1)); self.assertIsNone(_safe_regular_text(log))

    def test_artifact_scan_rejects_goal_tokens_paths_env_and_output_canaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); correlation = "corr_789"; self._write_record(root, self._record(correlation)); output = root / "artifacts/compatibility-report.json"
            with patch("scripts.run_acceptance.subprocess.run", return_value=self._cw_error(correlation)):
                _write_diagnostic(output, self._failure(root), base=root, source_commit="unused")
            serialized = output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8")
        for canary in self.canaries:
            self.assertNotIn(canary, serialized)
        self.assertNotIn("--goal", serialized)
        self.assertIn('"stage": "plan.create"', serialized)

    def test_cw_error_failure_missing_logs_and_harness_exception_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("scripts.run_acceptance.subprocess.run", return_value=subprocess.CompletedProcess(["cw"], 7, "", "")):
                self.assertEqual("unavailable", _capture_cw_diagnostic(self._failure(root))["diagnostic_status"])
            output = root / "artifacts/compatibility-report.json"; _write_diagnostic(output, RuntimeError("private payload"), base=root, source_commit="unused")
            diagnostic = json.loads(output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual("acceptance.harness", diagnostic["stage"])
        self.assertEqual("unavailable", diagnostic["diagnostic_status"])

    def test_timeout_uses_declared_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary, patch("scripts.run_acceptance.subprocess.run", side_effect=subprocess.TimeoutExpired(["cw"], 17)), self.assertRaises(AcceptanceFailure) as raised:
            _run(["cw", "retry"], cwd=Path(temporary), environment={}, timeout=17, diagnostic_stage="reviewer.retry", diagnostic_executable="cw", diagnostic_command="retry")
        self.assertTrue(raised.exception.timed_out)
        self.assertEqual("reviewer.retry", raised.exception.stage)

    def test_text_traceback_allows_only_relative_cw_frames_on_windows_and_posix(self):
        trace = ('Traceback (most recent call last):\n'
                 '  File "C:\\Users\\RUNNER~1\\site-packages\\cw\\cli\\runner.py", line 71, in run\n'
                 '    private source\n'
                 '  File "/home/runner/site-packages/cw/checks/verification.py", line 19, in execute\n'
                 '    private source\n'
                 '  File "/outside/private.py", line 1, in leak\n'
                 'ValueError: private message')
        frames = _text_traceback_frames(trace, "ValueError")
        self.assertEqual(["cw/cli/runner.py", "cw/checks/verification.py"], [frame["module"] for frame in frames])
        self.assertNotIn("private", json.dumps(frames))
        self.assertEqual([], _text_traceback_frames('File "/outside/private.py", line 1, in leak', "ValueError"))
        self.assertEqual("ValueError", _text_traceback_frames(
            'File "C:\\site-packages\\cw\\cli\\runner.py", line 7, in run\nValueError: private', None,
        )[-1]["exception_type"])

    def test_canonical_root_is_existing_identity_and_rejects_unrelated_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); alias = root / "."
            self.assertTrue(os.path.samefile(_canonical_root(root), alias))
            other = root / "other"; other.mkdir()
            self.assertFalse(os.path.samefile(_canonical_root(root), other))

    def test_binding_artifact_uses_boolean_allowlist_and_closed_reason_enum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); output = root / "artifacts/compatibility-report.json"
            _write_diagnostic(output, self._failure(root), base=root, source_commit="unused")
            diagnostic = json.loads(output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8"))
        fields = ("canonical_root_available", "project_metadata_present", "envelope_code_present", "envelope_correlation_present", "last_error_changed", "last_error_safe_regular", "record_found", "correlation_match", "code_match", "traceback_frame_available")
        self.assertTrue(all(type(diagnostic[field]) is bool for field in fields))
        self.assertEqual("project_metadata_missing", diagnostic["binding_failure_reason"])
        diagnostic["binding_failure_reason"] = "not-an-enum"
        with self.assertRaises(AcceptanceFailure):
            _validate_diagnostic(diagnostic)


if __name__ == "__main__":
    unittest.main()
