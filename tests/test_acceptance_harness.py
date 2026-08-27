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
    _interrupt,
    _operation_stage,
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

    def _failure(self, root: Path, correlation: str = "41e0163899520133") -> AcceptanceFailure:
        return AcceptanceFailure("raw failure", stage="plan.create", executable="cw", command_name="plan", exit_code=1, executable_path="cw", cwd=root, environment={"PRIVATE_ENV": self.canaries[2]}, envelope_code="INTERNAL_ERROR", envelope_correlation=correlation, error_fingerprint="before")

    def _record(self, correlation: str) -> dict[str, object]:
        return {"correlation_id": correlation, "code": "INTERNAL_ERROR", "message": self.canaries[1], "source": "plan", "exception_type": "ValueError", "traceback": [{"path": r"C:\host\site-packages\cw\cli\runner.py", "function": "run", "line": 73, "exception_type": "ValueError", "message": self.canaries[2]}]}

    def _write_record(self, root: Path, record: dict[str, object]) -> None:
        (root / ".cw/logs").mkdir(parents=True)
        (root / ".cw/logs/last-error.json").write_text(json.dumps(record), encoding="utf-8")

    def _cw_error(self, correlation: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["cw", "error"], 0, json.dumps({"data": {"correlation_id": correlation}}), "")

    class _InterruptProcess:
        def __init__(self, returncode: int = 130) -> None:
            self.pid = 991
            self.returncode = returncode
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else self.returncode

        def kill(self) -> None:
            self.running = False

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            self.running = False
            return "STDOUT_PRIVATE_CANARY", "STDERR_PRIVATE_CANARY"

        def send_signal(self, _signal: int) -> None:
            return None

    def _interrupt_failure(
        self,
        *,
        returncode: int = 130,
        child_ready: bool = True,
        child_alive: list[bool] | None = None,
        monotonic: list[float] | None = None,
        gate: bool = False,
        retry_error: bool = False,
        recovered_status: str = "COMPLETED",
    ) -> AcceptanceFailure | None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            (root / ".cw/runtime").mkdir(parents=True)
            if child_ready:
                (root / ".cw/runtime/active-run.json").write_text(
                    json.dumps({"process_pid": 991}), encoding="utf-8",
                )
            if gate:
                gates = root / ".cw/gates"
                gates.mkdir()
                (gates / "phase.approved.json").write_text("{}", encoding="utf-8")
            process = self._InterruptProcess(returncode)
            alive = list(child_alive if child_alive is not None else [True, True, False, False])
            clock = iter(monotonic if monotonic is not None else [0.0, 0.0, 0.0])
            retry = AcceptanceFailure("private retry failure") if retry_error else None
            with patch("scripts.run_acceptance._repository", return_value=root), patch(
                "scripts.run_acceptance._prepare_plan"
            ), patch("scripts.run_acceptance.subprocess.Popen", return_value=process), patch(
                "scripts.run_acceptance.os.killpg"
            ), patch("scripts.run_acceptance.process_is_alive", side_effect=lambda _pid: alive.pop(0) if alive else False), patch(
                "scripts.run_acceptance.time.monotonic", side_effect=lambda: next(clock)), patch(
                "scripts.run_acceptance.time.sleep"
            ), patch(
                "scripts.run_acceptance.os.kill"
            ), patch("scripts.run_acceptance._run", side_effect=retry), patch(
                "scripts.run_acceptance._state", return_value={"status": recovered_status}
            ):
                try:
                    _interrupt(Path("cw"), root.parent, {})
                except AcceptanceFailure as error:
                    return error
        return None

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
            root = Path(temporary); correlation = "41e0163899520133"; self._write_record(root, self._record(correlation))
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
            root = Path(temporary); correlation = "41e0163899520133"; record = self._record(correlation); record["source"] = "status"; self._write_record(root, record)
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

    def test_operation_context_classifies_default_command_failure(self):
        completed = subprocess.CompletedProcess(["python"], 1, "", "")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "scripts.run_acceptance.subprocess.run", return_value=completed,
        ), self.assertRaises(AcceptanceFailure) as raised, _operation_stage(
            "acceptance.operation.second_run",
        ):
            _run(["python"], cwd=Path(temporary), environment={})
        self.assertEqual("acceptance.operation.second_run", raised.exception.stage)

    def test_interrupt_process_start_oserror_has_safe_child_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            with patch("scripts.run_acceptance._repository", return_value=root), patch(
                "scripts.run_acceptance._prepare_plan"
            ), patch(
                "scripts.run_acceptance.subprocess.Popen", side_effect=PermissionError("private path")
            ), self.assertRaises(AcceptanceFailure) as raised:
                _interrupt(Path("cw"), root.parent, {})
        self.assertEqual("interrupt.child_start", raised.exception.stage)
        self.assertNotIn("private path", str(raised.exception))

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

    def test_interrupt_failures_have_specific_safe_stages(self):
        cases = {
            "interrupt.child_start": {"child_ready": False, "monotonic": [0.0, 16.0]},
            "interrupt.parent_exit": {"returncode": 1},
            "interrupt.child_cleanup": {"child_alive": [True, True, True, True, True], "monotonic": [0.0, 0.0, 0.0, 6.0]},
            "interrupt.partial_gate": {"gate": True},
            "interrupt.retry": {"retry_error": True},
            "interrupt.recovery": {"recovered_status": "ERROR"},
        }
        for expected_stage, kwargs in cases.items():
            with self.subTest(stage=expected_stage):
                failure = self._interrupt_failure(**kwargs)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(expected_stage, failure.stage)
                self.assertRegex(failure.stage, r"^interrupt\.[a-z_]+$")
                self.assertNotIn("STDOUT_PRIVATE_CANARY", str(failure))
                self.assertNotIn("STDERR_PRIVATE_CANARY", str(failure))

    def test_interrupt_success_remains_pass_and_failure_artifact_is_redacted(self):
        self.assertIsNone(self._interrupt_failure())
        failure = self._interrupt_failure(child_ready=False, monotonic=[0.0, 16.0])
        assert failure is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "artifacts/compatibility-report.json"
            _write_diagnostic(output, failure, base=root, source_commit="unused")
            artifact = output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8")
        self.assertIn('"stage": "interrupt.child_start"', artifact)
        for private_value in (*self.canaries, "STDOUT_PRIVATE_CANARY", "STDERR_PRIVATE_CANARY", "991"):
            self.assertNotIn(private_value, artifact)


if __name__ == "__main__":
    unittest.main()
