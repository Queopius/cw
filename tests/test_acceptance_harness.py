from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from scripts.run_acceptance import (
    AcceptanceFailure,
    _canonical_root,
    _capture_cw_diagnostic,
    _environment,
    _first_run_defaults,
    _first_run_envelope_metadata,
    _fixture_evidence,
    _install_fake_codex,
    _interrupt,
    _InvocationKind,
    _json_object,
    _managed_child_is_running,
    _operation_stage,
    _result,
    _review_hook_failure_stage,
    _run,
    _run_first_phase,
    _run_second_phase,
    _safe_fixed_file_present,
    _safe_regular_text,
    _second_run_defaults,
    _second_run_envelope_metadata,
    _single_phase,
    _single_phase_cycles,
    _single_state_failure_stage,
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
        (root / ".cw/logs").mkdir(parents=True, exist_ok=True)
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
            retry = AcceptanceFailure("private retry failure", stage="interrupt.retry") if retry_error else None
            with patch("scripts.run_acceptance._repository", return_value=root), patch(
                "scripts.run_acceptance._prepare_plan"
            ), patch("scripts.run_acceptance.subprocess.Popen", return_value=process), patch(
                "scripts.run_acceptance.os.killpg"
            ), patch("scripts.run_acceptance._managed_child_is_running", side_effect=lambda _pid: alive.pop(0) if alive else False), patch(
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

    def _single_failure(
        self, *, state: dict[str, object] | None = None, gates: int = 1,
        status: str = '{"state":"COMPLETED"}', inspect: str = '{"run":{"run_id":"run-1"}}',
        command_failure: str | None = None, evidence: str | None = None,
        evidence_symlink: bool = False,
    ) -> AcceptanceFailure | None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"; gates_dir = root / ".cw/gates"; gates_dir.mkdir(parents=True)
            for index in range(gates):
                (gates_dir / f"{index}.approved.json").write_text("{}", encoding="utf-8")
            evidence_paths = {
                "completion": root / ".cw/completion/completion.satisfied.json",
                "readiness": root / ".cw/runtime/READY_FOR_REVIEW.json",
            }
            if evidence is not None:
                path = evidence_paths[evidence]
                path.parent.mkdir(parents=True, exist_ok=True)
                if evidence_symlink:
                    target = root / "unsafe-evidence.json"
                    target.write_text("{}", encoding="utf-8")
                    path.symlink_to(target)
                else:
                    path.write_text("{}", encoding="utf-8")
            values = {"status": status, "inspect": inspect}
            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                name = command[1] if len(command) > 1 else "run"
                if name == command_failure:
                    raise AcceptanceFailure("private command failure", stage=f"acceptance.operation.{name}_command")
                if name == "status":
                    return subprocess.CompletedProcess(command, 0, values["status"], "")
                if name == "inspect":
                    return subprocess.CompletedProcess(command, 0, values["inspect"], "")
                return subprocess.CompletedProcess(command, 0, "{}", "")
            with patch("scripts.run_acceptance._repository", return_value=root), patch(
                "scripts.run_acceptance._prepare_plan"
            ), patch("scripts.run_acceptance._run", side_effect=fake_run), patch(
                "scripts.run_acceptance._state", return_value=state or {"status": "COMPLETED", "current_phase": None}
            ):
                try:
                    _single_phase(Path("cw"), root.parent, {})
                except AcceptanceFailure as error:
                    return error
        return None

    def test_report_and_environment_contracts_remain(self):
        self.assertEqual("PASS", _result("PASS")["status"])
        with self.assertRaises(ValueError): _result("MAYBE")
        with self.assertRaises(AcceptanceFailure): _validate_report({"schema_version": 1})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime = root / "runtime"; runtime_bin = runtime / ("Scripts" if os.name == "nt" else "bin"); runtime_bin.mkdir(parents=True)
            cw = runtime_bin / ("cw.exe" if os.name == "nt" else "cw")
            cw.write_text("fixture", encoding="utf-8")
            cw.chmod(0o755)
            environment = _environment(root, runtime, root)
            self.assertNotIn("CODEX_HOME", environment)
            self.assertTrue(os.path.samefile(cw, environment["CW_ACCEPTANCE_CW_EXECUTABLE"]))
            self.assertTrue(os.path.samefile(runtime, environment["CW_ACCEPTANCE_RUNTIME_ROOT"]))
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

    def test_single_phase_final_contracts_have_distinct_stages(self):
        cases = {
            "acceptance.operation.single_state.error": {"state": {"status": "ERROR", "current_phase": None}},
            "acceptance.operation.single_gate": {"gates": 0},
            "acceptance.operation.status_json": {"status": "not json"},
            "acceptance.operation.status_contract": {"status": '{"state":"ERROR"}'},
            "acceptance.operation.inspect_json": {"inspect": "not json"},
            "acceptance.operation.inspect_contract": {"inspect": '{"run":{}}'},
            "acceptance.operation.history_command": {"command_failure": "history"},
            "acceptance.operation.logs_command": {"command_failure": "logs"},
            "acceptance.operation.doctor_command": {"command_failure": "doctor"},
        }
        for stage, kwargs in cases.items():
            with self.subTest(stage=stage):
                failure = self._single_failure(**kwargs)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(stage, failure.stage)
        self.assertIsNone(self._single_failure())

    def test_single_state_failures_have_closed_safe_classifications(self):
        cases = (
            ("completed_phase_present", {"status": "COMPLETED", "current_phase": "PRIVATE_PHASE_CANARY"}, None),
            ("planned_complete_gate_present", {"status": "PLANNED_COMPLETE", "current_phase": None}, "completion"),
            ("planned_complete_gate_absent", {"status": "PLANNED_COMPLETE", "current_phase": None}, None),
            (
                "review_hook.no_review",
                {"status": "IN_PROGRESS", "current_phase": "PRIVATE_PHASE_CANARY"},
                "readiness",
            ),
            ("in_progress_readiness_absent", {"status": "IN_PROGRESS", "current_phase": "PRIVATE_PHASE_CANARY"}, None),
            ("ready_for_review", {"status": "READY_FOR_REVIEW", "current_phase": "PRIVATE_PHASE_CANARY"}, None),
            ("reviewing", {"status": "REVIEWING", "current_phase": "PRIVATE_PHASE_CANARY"}, None),
            ("error", {"status": "ERROR", "current_phase": None}, None),
            ("other", {"status": "PAUSED", "current_phase": None}, None),
        )
        for suffix, state, evidence in cases:
            with self.subTest(suffix=suffix):
                failure = self._single_failure(state=state, evidence=evidence)
                self.assertIsNotNone(failure)
                assert failure is not None
                expected = (
                    f"acceptance.operation.{suffix}"
                    if suffix.startswith("review_hook.")
                    else f"acceptance.operation.single_state.{suffix}"
                )
                self.assertEqual(expected, failure.stage)
                self.assertNotIn("PRIVATE_PHASE_CANARY", str(failure))

    def test_single_state_evidence_rejects_symlinks_without_reading_them(self):
        for state, evidence in (
            ({"status": "PLANNED_COMPLETE", "current_phase": None}, "completion"),
            ({"status": "IN_PROGRESS", "current_phase": "phase"}, "readiness"),
        ):
            with self.subTest(evidence=evidence):
                failure = self._single_failure(
                    state=state, evidence=evidence, evidence_symlink=True,
                )
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual("acceptance.operation.single_state.other", failure.stage)

    def test_fixed_evidence_presence_uses_only_fixed_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = Path(".cw/runtime/READY_FOR_REVIEW.json")
            self.assertFalse(_safe_fixed_file_present(root, relative))
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(self.canaries[0], encoding="utf-8")
            self.assertTrue(_safe_fixed_file_present(root, relative))
            self.assertEqual(
                "acceptance.operation.review_hook.no_review",
                _single_state_failure_stage(
                    root,
                    {"status": "IN_PROGRESS"},
                    "phase",
                ),
            )
            with self.assertRaises(ValueError):
                _safe_fixed_file_present(root, Path("../PRIVATE_PHASE_CANARY"))

    def test_single_state_artifact_contains_only_safe_classification(self):
        failure = self._single_failure(
            state={"status": "IN_PROGRESS", "current_phase": "PRIVATE_PHASE_CANARY"},
            evidence="readiness",
        )
        assert failure is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "artifacts/compatibility-report.json"
            _write_diagnostic(output, failure, base=root, source_commit="unused")
            artifact = output.with_name("compatibility-diagnostic.json").read_text(
                encoding="utf-8",
            )
        self.assertIn(
            '"stage": "acceptance.operation.review_hook.no_review"',
            artifact,
        )
        for private_value in (*self.canaries, "PRIVATE_PHASE_CANARY", "READY_FOR_REVIEW.json"):
            self.assertNotIn(private_value, artifact)

    def test_review_hook_failure_evidence_has_closed_safe_classifications(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            phase = "01-phase-1"
            state = {
                "status": "IN_PROGRESS",
                "current_phase": phase,
                "last_review": None,
                "history": [],
            }
            self.assertEqual(
                "acceptance.operation.review_hook.no_review",
                _review_hook_failure_stage(root, state, phase),
            )

            review = root / ".cw/reviews/review.json"
            review.parent.mkdir(parents=True)
            review.write_text("{}", encoding="utf-8")
            state["last_review"] = ".cw/reviews/review.json"
            self.assertEqual(
                "acceptance.operation.review_hook.review_without_gate",
                _review_hook_failure_stage(root, state, phase),
            )

            gate = root / f".cw/gates/{phase}.approved.json"
            gate.parent.mkdir(parents=True)
            gate.write_text("{}", encoding="utf-8")
            self.assertEqual(
                "acceptance.operation.review_hook.gate_without_advance",
                _review_hook_failure_stage(root, state, phase),
            )

            state["history"] = [{"action": "approved", "phase": phase}]
            self.assertEqual(
                "acceptance.operation.review_hook.state_regressed",
                _review_hook_failure_stage(root, state, phase),
            )

            completion = root / ".cw/completion/completion.satisfied.json"
            completion.parent.mkdir(parents=True)
            completion.write_text("{}", encoding="utf-8")
            self.assertEqual(
                "acceptance.operation.review_hook.completion_without_state",
                _review_hook_failure_stage(root, state, phase),
            )

            self.assertEqual(
                "acceptance.operation.review_hook.unknown",
                _review_hook_failure_stage(root, state, None),
            )

    def test_review_hook_evidence_rejects_symlink_and_hardlink(self):
        state = {
            "status": "IN_PROGRESS",
            "current_phase": "phase",
            "last_review": ".cw/reviews/review.json",
            "history": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            review = root / ".cw/reviews/review.json"
            review.parent.mkdir(parents=True)
            try:
                review.symlink_to(target)
            except OSError:
                self.assertEqual("nt", os.name)
            else:
                self.assertEqual(
                    "acceptance.operation.review_hook.unknown",
                    _review_hook_failure_stage(root, state, "phase"),
                )
                review.unlink()
            os.link(target, review)
            self.assertEqual(
                "acceptance.operation.review_hook.unknown",
                _review_hook_failure_stage(root, state, "phase"),
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            gate = root / ".cw/gates/phase.approved.json"
            gate.parent.mkdir(parents=True)
            try:
                gate.symlink_to(target)
            except OSError:
                self.assertEqual("nt", os.name)
            else:
                state["last_review"] = None
                self.assertEqual(
                    "acceptance.operation.review_hook.unknown",
                    _review_hook_failure_stage(root, state, "phase"),
                )
                gate.unlink()
            os.link(target, gate)
            self.assertEqual(
                "acceptance.operation.review_hook.unknown",
                _review_hook_failure_stage(root, state, "phase"),
            )

    def test_single_phase_cycle_policy_and_roots_are_platform_specific(self):
        with patch("scripts.run_acceptance.os.name", "nt"):
            self.assertEqual((1, 2, 3, 4, 5), _single_phase_cycles())
        with patch("scripts.run_acceptance.os.name", "posix"):
            self.assertEqual((None,), _single_phase_cycles())

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            roots: list[str] = []

            def repository(_base: Path, name: str, _environment: dict[str, str]) -> Path:
                roots.append(name)
                root = base / name
                (root / ".cw/gates").mkdir(parents=True)
                (root / ".cw/gates/phase.approved.json").write_text("{}", encoding="utf-8")
                return root

            def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if len(command) > 1 and command[1] == "status":
                    value = '{"state":"COMPLETED"}'
                elif len(command) > 1 and command[1] == "inspect":
                    value = '{"run":{"run_id":"run-1"}}'
                else:
                    value = "{}"
                return subprocess.CompletedProcess(command, 0, value, "")

            original_environment: dict[str, str] = {}
            with patch("scripts.run_acceptance._repository", side_effect=repository), patch(
                "scripts.run_acceptance._prepare_plan"
            ), patch("scripts.run_acceptance._run", side_effect=run), patch(
                "scripts.run_acceptance._state",
                return_value={"status": "COMPLETED", "current_phase": None},
            ):
                for cycle in (1, 2, 3, 4, 5):
                    _single_phase(Path("cw"), base, original_environment, cycle=cycle)
            self.assertEqual(
                [f"single phase {cycle}" for cycle in range(1, 6)],
                roots,
            )
            self.assertEqual({}, original_environment)

    def test_json_object_rejects_concatenation_and_non_object_without_output(self):
        for value in ("{}{}", "[]", "null"):
            with self.subTest(value=value), self.assertRaises(AcceptanceFailure) as raised:
                _json_object(value, stage="acceptance.operation.status_json")
            self.assertEqual("acceptance.operation.status_json", raised.exception.stage)
            self.assertNotIn(value, str(raised.exception))

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

    def test_second_run_envelope_classification_is_single_document_and_allowlisted(self):
        correlation = "41e0163899520133"
        error = _second_run_envelope_metadata(json.dumps({
            "error": {"code": "INTEGRITY_ERROR", "correlation_id": correlation},
        }))
        self.assertEqual("error", error["second_run_envelope_kind"])
        self.assertEqual("INTEGRITY_ERROR", error["second_run_error_code"])
        self.assertTrue(error["second_run_error_code_present"])
        self.assertTrue(error["second_run_correlation_hash_present"])
        self.assertNotIn(correlation, json.dumps(error))
        self.assertEqual(
            "success",
            _second_run_envelope_metadata(json.dumps({"ok": True, "data": {}}))[
                "second_run_envelope_kind"
            ],
        )
        self.assertEqual("missing", _second_run_envelope_metadata("")["second_run_envelope_kind"])
        for value in ("{", "{}{}", "[]"):
            self.assertEqual(
                "invalid",
                _second_run_envelope_metadata(value)["second_run_envelope_kind"],
            )

    def test_second_run_preserves_first_safe_failed_condition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".cw/runtime").mkdir(parents=True)
            (root / ".cw/gates").mkdir()
            state_path = root / ".cw/state.json"
            state_path.write_text(
                json.dumps({"status": "READY", "current_phase": "01-phase"}),
                encoding="utf-8",
            )
            readiness = root / ".cw/runtime/READY_FOR_REVIEW.json"
            readiness.write_text("{}", encoding="utf-8")
            failure = AcceptanceFailure(
                "private process output",
                stage="acceptance.operation.second_run",
                executable="cw",
                command_name="run",
                exit_code=1,
                second_run={
                    **_second_run_defaults(),
                    "second_run_envelope_kind": "success",
                },
            )
            with patch("scripts.run_acceptance._run", side_effect=failure) as invoked, self.assertRaises(
                AcceptanceFailure,
            ) as raised:
                _run_second_phase(Path("cw"), ["run", "3"], root=root, environment={})
        metadata = raised.exception.second_run
        self.assertEqual(["cw", "run", "3"], invoked.call_args.args[0])
        self.assertNotIn("--output=json", invoked.call_args.args[0])
        self.assertEqual("readiness_not_consumed", metadata["second_run_failure_reason"])
        self.assertTrue(metadata["readiness_before"])
        self.assertTrue(metadata["readiness_after"])
        self.assertFalse(metadata["gate_after"])
        self.assertFalse(metadata["phase_changed"])
        self.assertFalse(metadata["hook_postcondition_passed"])

    def test_second_run_artifact_contains_only_typed_safe_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "artifacts/compatibility-report.json"
            failure = self._failure(root)
            failure.stage = "acceptance.operation.second_run"
            failure.invocation = _InvocationKind.SECOND_RUN
            failure.second_run = {
                **_second_run_defaults(),
                "second_run_envelope_kind": "error",
                "second_run_error_code_present": True,
                "second_run_error_code": "INTEGRITY_ERROR",
                "second_run_failure_reason": "hook_error",
            }
            _write_diagnostic(output, failure, base=root, source_commit="unused")
            diagnostic = json.loads(
                output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8")
            )
        booleans = (
            "second_run_exit_expected", "second_run_error_code_present",
            "second_run_correlation_hash_present", "phase_changed",
            "readiness_before", "readiness_after", "gate_before", "gate_after",
            "hook_postcondition_passed",
        )
        self.assertTrue(all(type(diagnostic[key]) is bool for key in booleans))
        self.assertEqual("hook_error", diagnostic["second_run_failure_reason"])
        self.assertNotIn(self.canaries[0], json.dumps(diagnostic))
        for reason in (
            "process_exit", "envelope_missing", "envelope_invalid", "hook_error",
            "state_not_advanced", "readiness_not_consumed", "gate_missing",
            "phase_mismatch", "unexpected_terminal_state", "artifact_binding_failed", "none",
        ):
            diagnostic["second_run_failure_reason"] = reason
            _validate_diagnostic(diagnostic)
        diagnostic["second_run_failure_reason"] = "private-reason"
        with self.assertRaises(AcceptanceFailure):
            _validate_diagnostic(diagnostic)

    def test_first_run_envelope_is_single_document_allowlisted_and_hashed(self):
        correlation = "41e0163899520133"
        error = _first_run_envelope_metadata(json.dumps({
            "error": {"code": "INTEGRITY_ERROR", "correlation_id": correlation},
        }))
        self.assertEqual("error", error["first_run_envelope_kind"])
        self.assertEqual("INTEGRITY_ERROR", error["first_run_error_code"])
        self.assertTrue(error["first_run_error_code_present"])
        self.assertTrue(error["first_run_correlation_hash_present"])
        self.assertNotIn(correlation, json.dumps(error))
        self.assertEqual(
            "success",
            _first_run_envelope_metadata(json.dumps({"ok": True}))["first_run_envelope_kind"],
        )
        self.assertEqual("missing", _first_run_envelope_metadata("")["first_run_envelope_kind"])
        for value in ("{", "{}{}", "[]"):
            with self.subTest(value=value):
                self.assertEqual(
                    "invalid",
                    _first_run_envelope_metadata(value)["first_run_envelope_kind"],
                )
        unknown = _first_run_envelope_metadata(json.dumps({
            "error": {"code": "PRIVATE_ERROR", "correlation_id": correlation},
        }))
        self.assertFalse(unknown["first_run_error_code_present"])
        self.assertIsNone(unknown["first_run_error_code"])
        malformed = _first_run_envelope_metadata(json.dumps({
            "error": {"code": "INTEGRITY_ERROR", "correlation_id": "not-safe"},
        }))
        self.assertFalse(malformed["first_run_correlation_hash_present"])

    def test_invocation_identity_prevents_cross_run_metadata_contamination(self):
        first_output = json.dumps({"error": {"code": "INTEGRITY_ERROR"}})
        second_output = json.dumps({"error": {"code": "INVALID_STATE"}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = (
                subprocess.CompletedProcess(["cw"], 1, first_output, self.canaries[-1]),
                subprocess.CompletedProcess(["cw", "run"], 1, second_output, self.canaries[-1]),
            )
            failures: list[AcceptanceFailure] = []
            with patch("scripts.run_acceptance.subprocess.run", side_effect=results):
                for invocation, stage in (
                    (_InvocationKind.FIRST_RUN, "acceptance.operation.first_run"),
                    (_InvocationKind.SECOND_RUN, "acceptance.operation.second_run"),
                ):
                    with self.assertRaises(AcceptanceFailure) as raised:
                        _run(
                            ["cw"], cwd=root, environment={},
                            diagnostic_stage=stage, diagnostic_executable="cw",
                            diagnostic_command="start", invocation=invocation,
                        )
                    failures.append(raised.exception)
            first_output_path = root / "first/compatibility-report.json"
            second_output_path = root / "second/compatibility-report.json"
            _write_diagnostic(first_output_path, failures[0], base=root, source_commit="unused")
            _write_diagnostic(second_output_path, failures[1], base=root, source_commit="unused")
            first = json.loads(
                first_output_path.with_name("compatibility-diagnostic.json").read_text(
                    encoding="utf-8",
                )
            )
            second = json.loads(
                second_output_path.with_name("compatibility-diagnostic.json").read_text(
                    encoding="utf-8",
                )
            )
        self.assertTrue(set(first) & set(_first_run_defaults()))
        self.assertFalse(any(key.startswith("second_run_") for key in first))
        self.assertFalse(any(key.startswith("first_run_") for key in second))
        self.assertTrue(any(key.startswith("second_run_") for key in second))
        for canary in self.canaries:
            self.assertNotIn(canary, json.dumps((first, second)))

    def test_first_and_second_run_success_preserve_their_exit_contracts(self):
        completed = subprocess.CompletedProcess(["cw"], 0, '{"ok":true}', "")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "scripts.run_acceptance.subprocess.run", side_effect=(completed, completed),
        ):
            root = Path(temporary)
            first = _run(
                ["cw"], cwd=root, environment={},
                diagnostic_stage="acceptance.operation.first_run",
                invocation=_InvocationKind.FIRST_RUN,
            )
            second = _run(
                ["cw", "run"], cwd=root, environment={},
                diagnostic_stage="acceptance.operation.second_run",
                invocation=_InvocationKind.SECOND_RUN,
            )
        self.assertEqual(0, first.returncode)
        self.assertEqual(0, second.returncode)

    def test_fixture_evidence_is_current_bounded_and_identity_bound(self):
        invocation_hash = sha256(b"invocation").hexdigest()
        payload = {
            "schema_version": 1,
            "invocation_sha256": invocation_hash,
            "last_stage": "hook_exit",
            "failure_reason": "hook_exit_nonzero",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".cw/runtime/acceptance-fixture-evidence.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                {"last_stage": "hook_exit", "failure_reason": "hook_exit_nonzero"},
                _fixture_evidence(root, invocation_hash, None),
            )
            fingerprint = sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
            self.assertIsNone(_fixture_evidence(root, invocation_hash, fingerprint))
            self.assertIsNone(_fixture_evidence(root, sha256(b"other").hexdigest(), None))
            path.write_text("{", encoding="utf-8")
            self.assertIsNone(_fixture_evidence(root, invocation_hash, None))
            path.write_bytes(b"x" * 4097)
            self.assertIsNone(_fixture_evidence(root, invocation_hash, None))

    def test_fixture_evidence_rejects_links_and_traversal(self):
        invocation_hash = sha256(b"invocation").hexdigest()
        payload = json.dumps({
            "schema_version": 1,
            "invocation_sha256": invocation_hash,
            "last_stage": "process_start",
            "failure_reason": "none",
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".cw/runtime"
            runtime.mkdir(parents=True)
            target = root / "target.json"
            target.write_text(payload, encoding="utf-8")
            path = runtime / "acceptance-fixture-evidence.json"
            try:
                path.symlink_to(target)
            except OSError:
                self.assertEqual("nt", os.name)
            else:
                self.assertIsNone(_fixture_evidence(root, invocation_hash, None))
                path.unlink()
            os.link(target, path)
            self.assertIsNone(_fixture_evidence(root, invocation_hash, None))
            path.unlink()
            with patch("scripts.run_acceptance._FIXTURE_EVIDENCE", Path("../target.json")):
                self.assertIsNone(_fixture_evidence(root, invocation_hash, None))

    def test_first_run_failure_preserves_fixture_and_binding_metadata(self):
        correlation = "41e0163899520133"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".cw/runtime").mkdir(parents=True)
            (root / ".cw/gates").mkdir()
            (root / ".cw/state.json").write_text(
                json.dumps({"status": "READY", "current_phase": "01-phase"}),
                encoding="utf-8",
            )

            def fail(_command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                environment = kwargs["environment"]
                assert isinstance(environment, dict)
                invocation_hash = sha256(
                    environment["CW_ACCEPTANCE_INVOCATION_ID"].encode("ascii"),
                ).hexdigest()
                evidence = root / ".cw/runtime/acceptance-fixture-evidence.json"
                evidence.write_text(json.dumps({
                    "schema_version": 1,
                    "invocation_sha256": invocation_hash,
                    "last_stage": "hook_exit",
                    "failure_reason": "hook_exit_nonzero",
                }), encoding="utf-8")
                raise AcceptanceFailure(
                    "private failure", stage="acceptance.operation.first_run",
                    executable="cw", command_name="start", exit_code=1,
                    executable_path="private-cw", cwd=root,
                    environment=environment, envelope_code="INTEGRITY_ERROR",
                    envelope_correlation=correlation,
                    invocation=_InvocationKind.FIRST_RUN,
                    first_run={
                        **_first_run_defaults(),
                        "first_run_envelope_kind": "error",
                        "first_run_error_code_present": True,
                        "first_run_error_code": "INTEGRITY_ERROR",
                        "first_run_correlation_hash_present": True,
                        "correlation_id_sha256": sha256(correlation.encode()).hexdigest(),
                    },
                )

            with patch("scripts.run_acceptance._run", side_effect=fail), self.assertRaises(
                AcceptanceFailure,
            ) as raised:
                _run_first_phase(Path("cw"), root=root, environment={})
            output = root / "artifacts/compatibility-report.json"
            _write_diagnostic(output, raised.exception, base=root, source_commit="unused")
            diagnostic = json.loads(
                output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8")
            )
        self.assertIs(raised.exception.invocation, _InvocationKind.FIRST_RUN)
        self.assertTrue(diagnostic["first_run_fixture_evidence_present"])
        self.assertEqual("hook_exit", diagnostic["first_run_fixture_last_stage"])
        self.assertEqual("hook_exit", diagnostic["first_run_failure_reason"])
        self.assertEqual("INTEGRITY_ERROR", diagnostic["first_run_error_code"])
        self.assertTrue(diagnostic["first_run_correlation_hash_present"])
        self.assertNotIn(correlation, json.dumps(diagnostic))
        self.assertFalse(any(key.startswith("second_run_") for key in diagnostic))
        for private in (*self.canaries, "private-cw", "CW_ACCEPTANCE_INVOCATION_ID"):
            self.assertNotIn(private, json.dumps(diagnostic))

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

    def test_posix_zombie_child_is_not_reported_as_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary) / "proc" / "991"
            proc_root.mkdir(parents=True)
            (proc_root / "stat").write_text("991 (fake child) Z 1 1 1", encoding="utf-8")
            with patch("scripts.run_acceptance.process_is_alive", return_value=True):
                self.assertFalse(_managed_child_is_running(991, proc_root=proc_root.parent))
            (proc_root / "stat").write_text("991 (fake child) S 1 1 1", encoding="utf-8")
            with patch("scripts.run_acceptance.process_is_alive", return_value=True):
                self.assertTrue(_managed_child_is_running(991, proc_root=proc_root.parent))
            with patch("scripts.run_acceptance.process_is_alive", return_value=False):
                self.assertFalse(_managed_child_is_running(991, proc_root=proc_root.parent))
            (proc_root / "stat").unlink()
            with patch("scripts.run_acceptance.process_is_alive", side_effect=[True, False]):
                self.assertFalse(_managed_child_is_running(991, proc_root=proc_root.parent))
            with patch("scripts.run_acceptance.process_is_alive", side_effect=[True, True]):
                self.assertTrue(_managed_child_is_running(991, proc_root=proc_root.parent))

    def test_interrupt_retry_preserves_original_run_failure_metadata(self):
        """The retry failure must remain binding-capable after _interrupt()."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            (root / ".cw/runtime").mkdir(parents=True)
            (root / ".cw/runtime/active-run.json").write_text(
                json.dumps({"process_pid": 991}), encoding="utf-8",
            )
            process = self._InterruptProcess()
            correlation = sha256(
                b"retry\0INTERNAL_ERROR\0Unexpected internal failure",
            ).hexdigest()[:16]
            original = AcceptanceFailure(
                "private retry failure", stage="unexpected", executable="cw",
                command_name="retry", exit_code=1, executable_path="cw-private",
                cwd=root, environment={"PRIVATE_ENV": self.canaries[2]},
                envelope_code="INTERNAL_ERROR", envelope_correlation=correlation,
                error_fingerprint="before",
            )
            captured: dict[str, object] = {}

            def retry(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["command"] = command
                captured.update(kwargs)
                raise original

            alive = iter([True, True, False, False])
            with patch("scripts.run_acceptance._repository", return_value=root), patch(
                "scripts.run_acceptance._prepare_plan"
            ), patch("scripts.run_acceptance.subprocess.Popen", return_value=process), patch(
                "scripts.run_acceptance.os.killpg"
            ), patch(
                "scripts.run_acceptance._managed_child_is_running", side_effect=lambda _pid: next(alive, False)
            ), patch("scripts.run_acceptance.time.sleep"), patch(
                "scripts.run_acceptance._run", side_effect=retry
            ), patch("scripts.run_acceptance._state", return_value={"status": "COMPLETED"}), self.assertRaises(
                AcceptanceFailure
            ) as raised:
                _interrupt(Path("cw"), root.parent, {})

        self.assertIs(raised.exception, original)
        self.assertEqual("interrupt.retry", captured["diagnostic_stage"])
        self.assertEqual("cw", captured["diagnostic_executable"])
        self.assertEqual("retry", captured["diagnostic_command"])
        self.assertEqual(["cw", "retry", "--json"], captured["command"])
        self.assertEqual(1, raised.exception.exit_code)
        self.assertEqual("INTERNAL_ERROR", raised.exception.envelope_code)
        self.assertEqual(correlation, raised.exception.envelope_correlation)
        self.assertEqual("before", raised.exception.error_fingerprint)
        self.assertEqual(root, raised.exception.cwd)
        self.assertEqual(self.canaries[2], raised.exception.environment["PRIVATE_ENV"])

    def test_run_failure_binds_retry_record_without_publishing_private_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            logs = root / ".cw/logs"
            logs.mkdir(parents=True)
            (logs / "last-error.json").write_text('{"old": true}', encoding="utf-8")
            correlation = sha256(
                b"retry\0INTERNAL_ERROR\0Unexpected internal failure",
            ).hexdigest()[:16]
            completed = subprocess.CompletedProcess(
                ["cw", "retry", "--json"], 1,
                json.dumps({"error": {"code": "INTERNAL_ERROR", "correlation_id": correlation}}),
                self.canaries[7],
            )
            with patch("scripts.run_acceptance.subprocess.run", return_value=completed), self.assertRaises(
                AcceptanceFailure
            ) as raised:
                _run(
                    ["cw-private", "retry", "--json"], cwd=root,
                    environment={"PRIVATE_ENV": self.canaries[2]},
                    diagnostic_stage="interrupt.retry", diagnostic_executable="cw",
                    diagnostic_command="retry",
                )
            record = {
                "source": "retry", "code": "INTERNAL_ERROR",
                "message": "Unexpected internal failure", "correlation_id": correlation,
                "safe_traceback": {"version": 1, "exception_type": "OSError", "frames": [
                    {"module": "cw.recovery", "function": "retry", "line": 77},
                ]},
            }
            self._write_record(root, record)
            diagnostic = _capture_cw_diagnostic(raised.exception)
            output = root / "artifacts/compatibility-report.json"
            _write_diagnostic(output, raised.exception, base=root, source_commit="unused")
            artifact = output.with_name("compatibility-diagnostic.json").read_text(encoding="utf-8")

        self.assertEqual("interrupt.retry", raised.exception.stage)
        self.assertEqual(1, raised.exception.exit_code)
        self.assertEqual("INTERNAL_ERROR", raised.exception.envelope_code)
        self.assertEqual(correlation, raised.exception.envelope_correlation)
        self.assertTrue(diagnostic["project_metadata_present"])
        self.assertTrue(diagnostic["envelope_code_present"])
        self.assertTrue(diagnostic["envelope_correlation_present"])
        self.assertTrue(diagnostic["last_error_changed"])
        self.assertTrue(diagnostic["record_found"])
        self.assertTrue(diagnostic["correlation_match"])
        self.assertTrue(diagnostic["code_match"])
        self.assertEqual("captured", diagnostic["diagnostic_status"])
        self.assertNotIn(correlation, artifact)
        for private_value in (*self.canaries, "cw-private", "PRIVATE_ENV", "--json"):
            self.assertNotIn(private_value, artifact)

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
