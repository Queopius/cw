from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures.fake_codex import fake_codex

FAKE = Path(__file__).parent / "fixtures/fake_codex/fake_codex.py"


class FakeCodexContractTests(unittest.TestCase):
    def run_fake(self, arguments, *, role, scenario="success", cwd=None, timeout=10):
        environment = {
            **os.environ,
            f"CW_{role.upper()}_ACTIVE": "1",
            "CW_FAKE_CODEX_SCENARIO": scenario,
        }
        return subprocess.run(
            [sys.executable, str(FAKE), *arguments], cwd=cwd, env=environment,
            text=True, capture_output=True, timeout=timeout, check=False,
        )

    def test_planner_writes_valid_bounded_public_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "plan.json"
            completed = self.run_fake(
                ["--cd", str(root), "exec", "--output-last-message", str(output)],
                role="planner", cwd=root,
            )
            self.assertEqual(0, completed.returncode)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(1, len(payload["phases"]))
            self.assertNotIn("reasoning", output.read_text(encoding="utf-8").lower())

    def test_planner_failure_and_malformed_output_are_detectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); output = root / "plan.json"
            failed = self.run_fake(
                ["--cd", str(root), "exec", "--output-last-message", str(output)],
                role="planner", scenario="planner_failure", cwd=root,
            )
            self.assertNotEqual(0, failed.returncode)
            malformed = self.run_fake(
                ["--cd", str(root), "exec", "--output-last-message", str(output)],
                role="planner", scenario="malformed_output", cwd=root,
            )
            self.assertEqual(0, malformed.returncode)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(output.read_text(encoding="utf-8"))

    def test_unmanaged_invocation_fails_closed(self):
        completed = subprocess.run(
            [sys.executable, str(FAKE)], text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(0, completed.returncode)

    def _runtime_identity(self, root: Path) -> tuple[Path, dict[str, str]]:
        runtime = root / "runtime"
        runtime_bin = runtime / ("Scripts" if os.name == "nt" else "bin")
        runtime_bin.mkdir(parents=True)
        executable = runtime_bin / ("cw.exe" if os.name == "nt" else "cw")
        executable.write_text("fixture", encoding="utf-8")
        executable.chmod(0o755)
        return executable, {
            "CW_ACCEPTANCE_CW_EXECUTABLE": str(executable.resolve()),
            "CW_ACCEPTANCE_RUNTIME_ROOT": str(runtime.resolve()),
        }

    def _implementer_project(self, root: Path) -> None:
        (root / ".cw/runtime").mkdir(parents=True, exist_ok=True)
        (root / ".cw/state.json").write_text(
            json.dumps({
                "status": "IN_PROGRESS",
                "current_phase": "01-acceptance-1",
            }),
            encoding="utf-8",
        )
        workflow = root / ".codex/workflow"
        workflow.mkdir(parents=True, exist_ok=True)
        (workflow / "phases.yaml").write_text(
            json.dumps({"phases": [{
                "id": "01-acceptance-1", "artifacts": ["artifacts/result.txt"],
            }]}),
            encoding="utf-8",
        )

    def test_exact_runtime_executable_is_required_for_review_hook(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, environment = self._runtime_identity(root)
            with patch.dict(os.environ, environment, clear=False):
                self.assertTrue(os.path.samefile(executable, fake_codex._acceptance_cw_executable()))

            outside = root / "outside-cw"
            outside.write_text("fixture", encoding="utf-8")
            outside.chmod(0o755)
            environment["CW_ACCEPTANCE_CW_EXECUTABLE"] = str(outside.resolve())
            with patch.dict(os.environ, environment, clear=False), self.assertRaises(ValueError):
                fake_codex._acceptance_cw_executable()

    def test_hook_contract_accepts_only_one_nonempty_completed_object(self):
        valid = json.dumps({"continue": False, "stopReason": "private reason"})
        self.assertTrue(fake_codex._valid_hook_response(valid))
        for payload in (
            "{}",
            "{not-json",
            valid + valid,
            "[]",
            json.dumps({"continue": True, "stopReason": "private reason"}),
            json.dumps({"continue": False}),
        ):
            with self.subTest(payload_type=type(payload).__name__):
                self.assertFalse(fake_codex._valid_hook_response(payload))

    def test_implementer_reaches_hook_contract_without_publishing_hook_output(self):
        canary = "HOOK_PRIVATE_CANARY"
        valid = json.dumps({"continue": False, "stopReason": canary})
        invalid_values = (
            "{}", "{not-json", valid + valid, "[]",
            json.dumps({"continue": True, "stopReason": canary}),
            json.dumps({"continue": False}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)

            def durable_hook(*_args, **_kwargs):
                gate = root / ".cw/gates/01-acceptance-1.approved.json"
                gate.parent.mkdir(parents=True, exist_ok=True)
                gate.write_text("{}", encoding="utf-8")
                (root / ".cw/runtime/READY_FOR_REVIEW.json").unlink(missing_ok=True)
                (root / ".cw/state.json").write_text(
                    json.dumps({"status": "PLANNED_COMPLETE", "current_phase": None}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(["cw"], 0, valid, canary)

            with patch.dict(os.environ, environment, clear=False), patch(
                "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                side_effect=durable_hook,
            ) as invoked:
                self.assertEqual(0, fake_codex._implement(root, []))
            invoked.assert_called_once()
            self.assertEqual(str(Path(environment["CW_ACCEPTANCE_CW_EXECUTABLE"])), invoked.call_args.args[0][0])

            for hook_output in invalid_values:
                self._implementer_project(root)
                with self.subTest(output=hook_output[:8]), patch.dict(
                    os.environ, environment, clear=False,
                ), patch(
                    "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                    return_value=subprocess.CompletedProcess(["cw"], 0, hook_output, canary),
                ), io.StringIO() as stdout, io.StringIO() as stderr, contextlib.redirect_stdout(
                    stdout,
                ), contextlib.redirect_stderr(stderr):
                    self.assertEqual(
                        fake_codex._HOOK_CONTRACT_FAILURE,
                        fake_codex._implement(root, []),
                    )
                    self.assertEqual("", stdout.getvalue())
                    self.assertEqual(fake_codex._HOOK_FAILURE_MESSAGE + "\n", stderr.getvalue())
                    self.assertNotIn(canary, stderr.getvalue())

    def test_nonzero_hook_exit_is_fixed_and_redacted(self):
        canary = "STDERR_PRIVATE_CANARY"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)
            with patch.dict(os.environ, environment, clear=False), patch(
                "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                return_value=subprocess.CompletedProcess(["cw"], 7, "", canary),
            ), io.StringIO() as stdout, io.StringIO() as stderr, contextlib.redirect_stdout(
                stdout,
            ), contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    fake_codex._HOOK_CONTRACT_FAILURE,
                    fake_codex._review_hook(
                        root,
                        os.environ.copy(),
                        phase_id="01-acceptance-1",
                        next_phase=None,
                    ),
                )
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(fake_codex._HOOK_FAILURE_MESSAGE + "\n", stderr.getvalue())
                self.assertNotIn(canary, stderr.getvalue())

    def test_hook_requires_durable_next_and_final_phase_postconditions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._implementer_project(root)
            gate = root / ".cw/gates/01-acceptance-1.approved.json"
            gate.parent.mkdir(parents=True)
            gate.write_text("{}", encoding="utf-8")
            (root / ".cw/state.json").write_text(
                json.dumps({"status": "IN_PROGRESS", "current_phase": "02-acceptance-2"}),
                encoding="utf-8",
            )
            self.assertTrue(fake_codex._durable_review_postcondition(
                root, "01-acceptance-1", "02-acceptance-2",
            ))
            (root / ".cw/state.json").write_text(
                json.dumps({"status": "PLANNED_COMPLETE", "current_phase": None}),
                encoding="utf-8",
            )
            self.assertTrue(fake_codex._durable_review_postcondition(
                root, "01-acceptance-1", None,
            ))

    def test_valid_hook_response_without_durable_transition_fails_closed(self):
        canary = "HOOK_PRIVATE_CANARY"
        valid = json.dumps({"continue": False, "stopReason": canary})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                return_value=subprocess.CompletedProcess(["cw"], 0, valid, canary),
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = fake_codex._review_hook(
                    root,
                    os.environ.copy(),
                    phase_id="01-acceptance-1",
                    next_phase=None,
                )
            self.assertEqual(fake_codex._HOOK_POSTCONDITION_FAILURE, result)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual(fake_codex._HOOK_POSTCONDITION_MESSAGE + "\n", stderr.getvalue())
            self.assertNotIn(canary, stderr.getvalue())

    def test_batch_parent_may_own_the_deferred_review_transition(self):
        canary = "HOOK_PRIVATE_CANARY"
        valid = json.dumps({"continue": False, "stopReason": canary})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)
            readiness = root / ".cw/runtime/READY_FOR_REVIEW.json"
            readiness.write_text("{}", encoding="utf-8")
            environment["CW_ACCEPTANCE_PARENT_REVIEW"] = "1"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                return_value=subprocess.CompletedProcess(["cw"], 0, valid, canary),
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = fake_codex._review_hook(
                    root,
                    os.environ.copy(),
                    phase_id="01-acceptance-1",
                    next_phase=None,
                )
            self.assertEqual(0, result)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertNotIn(canary, stdout.getvalue() + stderr.getvalue())

    def test_batch_parent_handoff_rejects_incompatible_durable_evidence(self):
        valid = json.dumps({"continue": False, "stopReason": "private reason"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            environment["CW_ACCEPTANCE_PARENT_REVIEW"] = "1"
            cases = ("readiness_missing", "gate_present", "phase_changed", "error")
            for case in cases:
                with self.subTest(case=case):
                    self._implementer_project(root)
                    readiness = root / ".cw/runtime/READY_FOR_REVIEW.json"
                    readiness.unlink(missing_ok=True)
                    gate = root / ".cw/gates/01-acceptance-1.approved.json"
                    gate.unlink(missing_ok=True)
                    if case != "readiness_missing":
                        readiness.write_text("{}", encoding="utf-8")
                    if case == "gate_present":
                        gate.parent.mkdir(parents=True, exist_ok=True)
                        gate.write_text("{}", encoding="utf-8")
                    if case in {"phase_changed", "error"}:
                        status = "ERROR" if case == "error" else "IN_PROGRESS"
                        phase = "01-acceptance-1" if case == "error" else "02-acceptance-2"
                        (root / ".cw/state.json").write_text(
                            json.dumps({"status": status, "current_phase": phase}),
                            encoding="utf-8",
                        )
                    stderr = io.StringIO()
                    with patch.dict(os.environ, environment, clear=False), patch(
                        "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                        return_value=subprocess.CompletedProcess(["cw"], 0, valid, ""),
                    ), contextlib.redirect_stderr(stderr):
                        result = fake_codex._review_hook(
                            root,
                            os.environ.copy(),
                            phase_id="01-acceptance-1",
                            next_phase=None,
                        )
                    self.assertEqual(fake_codex._HOOK_POSTCONDITION_FAILURE, result)
                    self.assertEqual(fake_codex._HOOK_POSTCONDITION_MESSAGE + "\n", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
