from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
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
        valid = json.dumps({
            "continue": False,
            "stopReason": "CW phase review completed. Run: cw status",
        })
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
        valid = json.dumps({
            "continue": False,
            "stopReason": "CW phase review completed. Run: cw status",
        })
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
        valid = json.dumps({
            "continue": False,
            "stopReason": "CW phase review completed. Run: cw status",
        })
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
        valid = json.dumps({
            "continue": False,
            "stopReason": "CW phase review completed. Run: cw status",
        })
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

    def test_fixture_evidence_is_atomic_closed_and_invocation_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".cw/runtime").mkdir(parents=True)
            invocation = "a" * 64
            environment = {"CW_ACCEPTANCE_INVOCATION_ID": invocation}
            with patch.dict(os.environ, environment, clear=False):
                self.assertTrue(fake_codex._record_fixture_evidence(
                    root, "hook_exit", "hook_exit_nonzero",
                ))
            path = root / ".cw/runtime/acceptance-fixture-evidence.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({
                "schema_version", "invocation_sha256", "last_stage", "failure_reason",
                "cw_error_code", "correlation_sha256",
            }, set(payload))
            self.assertEqual(sha256(invocation.encode("ascii")).hexdigest(), payload["invocation_sha256"])
            self.assertEqual("hook_exit", payload["last_stage"])
            self.assertEqual("hook_exit_nonzero", payload["failure_reason"])
            self.assertIsNone(payload["cw_error_code"])
            self.assertIsNone(payload["correlation_sha256"])
            self.assertNotIn(invocation, path.read_text(encoding="utf-8"))

    def test_fixture_evidence_completes_partial_binary_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".cw/runtime").mkdir(parents=True)
            invocation = "b" * 64
            real_write = os.write
            real_open = os.open
            write_sizes: list[int] = []
            open_flags: list[int] = []
            binary_flag = 1 << 29

            def partial_write(descriptor: int, payload: bytes) -> int:
                chunk = payload[: max(1, len(payload) // 2)]
                written = real_write(descriptor, chunk)
                write_sizes.append(written)
                return written

            def safe_open(path: object, flags: int, mode: int = 0o777) -> int:
                open_flags.append(flags)
                return real_open(path, flags & ~binary_flag, mode)

            with patch.dict(os.environ, {"CW_ACCEPTANCE_INVOCATION_ID": invocation}, clear=False), patch.object(
                fake_codex.os, "O_BINARY", binary_flag, create=True,
            ), patch(
                "tests.fixtures.fake_codex.fake_codex.os.open", side_effect=safe_open,
            ), patch(
                "tests.fixtures.fake_codex.fake_codex.os.write", side_effect=partial_write,
            ):
                self.assertTrue(fake_codex._record_fixture_evidence(
                    root, "process_exit", "none",
                ))
            self.assertGreater(len(write_sizes), 1)
            self.assertTrue(open_flags[0] & binary_flag)
            payload = json.loads(
                (root / ".cw/runtime/acceptance-fixture-evidence.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual("process_exit", payload["last_stage"])

    def test_fixture_evidence_writer_rejects_symlink_and_hardlink_destinations(self):
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime = root / ".cw/runtime"
                runtime.mkdir(parents=True)
                destination = runtime / "acceptance-fixture-evidence.json"
                target = root / "target.json"
                target.write_text("{}", encoding="utf-8")
                if link_kind == "symlink":
                    try:
                        destination.symlink_to(target)
                    except OSError:
                        self.assertEqual("nt", os.name)
                        continue
                else:
                    os.link(target, destination)
                with patch.dict(
                    os.environ, {"CW_ACCEPTANCE_INVOCATION_ID": "f" * 64}, clear=False,
                ), self.assertRaises(OSError):
                    fake_codex._record_fixture_evidence(root, "process_start", "none")

    def test_hook_failures_persist_only_safe_first_failure_enums(self):
        cases = (
            (subprocess.CompletedProcess(["cw"], 7, "", "STDERR_PRIVATE_CANARY"), "hook_exit_nonzero"),
            (subprocess.CompletedProcess(["cw"], 0, "", ""), "hook_envelope_missing"),
            (subprocess.CompletedProcess(["cw"], 0, "{", ""), "hook_envelope_invalid"),
            (subprocess.CompletedProcess(["cw"], 0, "{}", ""), "hook_contract_rejected"),
        )
        for completed, expected in cases:
            with self.subTest(reason=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _executable, environment = self._runtime_identity(root)
                self._implementer_project(root)
                environment.update({
                    "CW_ACCEPTANCE_INVOCATION_ID": "c" * 64,
                    "CW_ACCEPTANCE_PROJECT_ROOT": str(root.resolve()),
                })
                with patch.dict(os.environ, environment, clear=False), patch(
                    "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                    return_value=completed,
                ), contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(0, fake_codex._review_hook(
                        root, os.environ.copy(), phase_id="01-acceptance-1", next_phase=None,
                    ))
                payload = json.loads(
                    (root / ".cw/runtime/acceptance-fixture-evidence.json").read_text(
                        encoding="utf-8",
                    )
                )
                self.assertEqual(expected, payload["failure_reason"])
                serialized = json.dumps(payload)
                self.assertNotIn("STDERR_PRIVATE_CANARY", serialized)
                self.assertNotIn(str(root), serialized)

    def test_controlled_hook_error_preserves_only_code_and_correlation_hash(self):
        correlation = "41e0163899520133"
        response = json.dumps({
            "continue": False,
            "stopReason": "Review hook durable postcondition failed. Run: cw status",
            "systemMessage": "Review hook durable postcondition failed. Run: cw status",
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)
            environment["CW_ACCEPTANCE_INVOCATION_ID"] = "d" * 64

            def controlled_error(*_args, **_kwargs):
                logs = root / ".cw/logs"
                logs.mkdir()
                (logs / "last-error.json").write_text(json.dumps({
                    "schema_version": 1,
                    "source": "review",
                    "code": "VERIFICATION_INFRASTRUCTURE_ERROR",
                    "correlation_id": correlation,
                    "message": "PRIVATE_MESSAGE",
                }), encoding="utf-8")
                return subprocess.CompletedProcess(["cw"], 0, response, "PRIVATE_STDERR")

            with patch.dict(os.environ, environment, clear=False), patch(
                "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                side_effect=controlled_error,
            ), contextlib.redirect_stderr(io.StringIO()):
                result = fake_codex._review_hook(
                    root,
                    os.environ.copy(),
                    phase_id="01-acceptance-1",
                    next_phase=None,
                )
            payload = json.loads(
                (root / ".cw/runtime/acceptance-fixture-evidence.json").read_text(
                    encoding="utf-8",
                )
            )
        self.assertEqual(fake_codex._HOOK_CONTRACT_FAILURE, result)
        self.assertEqual("hook_contract_rejected", payload["failure_reason"])
        self.assertEqual("VERIFICATION_INFRASTRUCTURE_ERROR", payload["cw_error_code"])
        self.assertEqual(sha256(correlation.encode("ascii")).hexdigest(), payload["correlation_sha256"])
        self.assertNotIn(correlation, json.dumps(payload))
        self.assertNotIn("PRIVATE", json.dumps(payload))

    def test_hook_spawn_handoff_and_completion_failures_are_distinct(self):
        valid = json.dumps({
            "continue": False,
            "stopReason": "CW phase review completed. Run: cw status",
        })
        cases = (
            (OSError("PRIVATE_PATH"), {}, False, "hook_spawn_failed"),
            (
                subprocess.CompletedProcess(["cw"], 0, valid, ""),
                {"CW_ACCEPTANCE_PARENT_REVIEW": "1"},
                False,
                "handoff_incompatible",
            ),
            (
                subprocess.CompletedProcess(["cw"], 0, valid, ""),
                {},
                True,
                "completion_not_written",
            ),
        )
        for result, extra, create_gate, expected in cases:
            with self.subTest(reason=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _executable, environment = self._runtime_identity(root)
                self._implementer_project(root)
                if create_gate:
                    gate = root / ".cw/gates/01-acceptance-1.approved.json"
                    gate.parent.mkdir(parents=True)
                    gate.write_text("{}", encoding="utf-8")
                environment.update({
                    "CW_ACCEPTANCE_INVOCATION_ID": "1" * 64,
                    "CW_ACCEPTANCE_PROJECT_ROOT": str(root.resolve()),
                    **extra,
                })
                with patch.dict(os.environ, environment, clear=False), patch(
                    "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                    side_effect=result if isinstance(result, OSError) else None,
                    return_value=None if isinstance(result, OSError) else result,
                ), contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(0, fake_codex._review_hook(
                        root, os.environ.copy(), phase_id="01-acceptance-1", next_phase=None,
                    ))
                evidence = json.loads(
                    (root / ".cw/runtime/acceptance-fixture-evidence.json").read_text(
                        encoding="utf-8",
                    )
                )
                self.assertEqual(expected, evidence["failure_reason"])
                self.assertNotIn("PRIVATE", json.dumps(evidence))

    def test_fixture_rejects_external_project_and_records_repository_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)
            other = Path(temporary) / "other"
            other.mkdir()
            environment.update({
                "CW_ACCEPTANCE_INVOCATION_ID": "d" * 64,
                "CW_ACCEPTANCE_PROJECT_ROOT": str(other.resolve()),
            })
            with patch.dict(os.environ, environment, clear=False), contextlib.redirect_stderr(
                io.StringIO(),
            ):
                self.assertEqual(fake_codex._FIXTURE_FAILURE, fake_codex._implement(root, []))
            payload = json.loads(
                (root / ".cw/runtime/acceptance-fixture-evidence.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual("repository_invalid", payload["failure_reason"])
            self.assertEqual("runtime_verified", payload["last_stage"])

    def test_fixture_records_runtime_and_readiness_failures_without_private_data(self):
        for case in ("runtime", "readiness"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _executable, environment = self._runtime_identity(root)
                self._implementer_project(root)
                environment.update({
                    "CW_ACCEPTANCE_INVOCATION_ID": "2" * 64,
                    "CW_ACCEPTANCE_PROJECT_ROOT": str(root.resolve()),
                })
                if case == "runtime":
                    environment["CW_ACCEPTANCE_CW_EXECUTABLE"] = str(
                        (root / "missing-cw").resolve(),
                    )
                    scenario = "success"
                    expected = ("process_start", "runtime_invalid", fake_codex._FIXTURE_FAILURE)
                else:
                    scenario = "missing_readiness"
                    expected = ("repository_verified", "readiness_missing", 0)
                with patch.dict(os.environ, {
                    **environment, "CW_FAKE_CODEX_SCENARIO": scenario,
                }, clear=False), contextlib.redirect_stderr(io.StringIO()):
                    result = fake_codex._implement(root, [])
                evidence = json.loads(
                    (root / ".cw/runtime/acceptance-fixture-evidence.json").read_text(
                        encoding="utf-8",
                    )
                )
                self.assertEqual(expected[2], result)
                self.assertEqual(expected[0], evidence["last_stage"])
                self.assertEqual(expected[1], evidence["failure_reason"])
                self.assertNotIn(str(root), json.dumps(evidence))

    def test_successful_implementer_records_process_exit_without_private_payload(self):
        canary = "HOOK_PRIVATE_CANARY"
        valid = json.dumps({
            "continue": False,
            "stopReason": "CW phase review completed. Run: cw status",
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            self._implementer_project(root)
            environment.update({
                "CW_ACCEPTANCE_INVOCATION_ID": "e" * 64,
                "CW_ACCEPTANCE_PROJECT_ROOT": str(root.resolve()),
            })

            def durable_hook(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                gate = root / ".cw/gates/01-acceptance-1.approved.json"
                gate.parent.mkdir(parents=True, exist_ok=True)
                gate.write_text("{}", encoding="utf-8")
                (root / ".cw/runtime/READY_FOR_REVIEW.json").unlink(missing_ok=True)
                (root / ".cw/state.json").write_text(
                    json.dumps({"status": "COMPLETED", "current_phase": None}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(["cw"], 0, valid, canary)

            with patch.dict(os.environ, environment, clear=False), patch(
                "tests.fixtures.fake_codex.fake_codex.subprocess.run",
                side_effect=durable_hook,
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, fake_codex._implement(root, []))
            evidence = (
                root / ".cw/runtime/acceptance-fixture-evidence.json"
            ).read_text(encoding="utf-8")
        payload = json.loads(evidence)
        self.assertEqual("process_exit", payload["last_stage"])
        self.assertEqual("none", payload["failure_reason"])
        self.assertNotIn(canary, evidence)

    def test_unexpected_fixture_exception_is_closed_and_redacted(self):
        canary = "GOAL_PRIVATE_CANARY"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _executable, environment = self._runtime_identity(root)
            (root / ".cw/runtime").mkdir(parents=True, exist_ok=True)
            (root / ".cw/state.json").write_text(canary, encoding="utf-8")
            environment.update({
                "CW_IMPLEMENTER_ACTIVE": "1",
                "CW_ACCEPTANCE_INVOCATION_ID": "3" * 64,
                "CW_ACCEPTANCE_PROJECT_ROOT": str(root.resolve()),
            })
            completed = subprocess.run(
                [sys.executable, str(FAKE), "--cd", str(root)],
                cwd=root, env={**os.environ, **environment}, text=True,
                capture_output=True, check=False,
            )
            evidence = (
                root / ".cw/runtime/acceptance-fixture-evidence.json"
            ).read_text(encoding="utf-8")
        self.assertEqual(fake_codex._FIXTURE_FAILURE, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertEqual(fake_codex._FIXTURE_FAILURE_MESSAGE + "\n", completed.stderr)
        self.assertEqual("unexpected_exception", json.loads(evidence)["failure_reason"])
        self.assertNotIn(canary, completed.stdout + completed.stderr + evidence)


if __name__ == "__main__":
    unittest.main()
