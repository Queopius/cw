from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from cw.adapters.codex import CodexAdapter
from cw.adapters.invocation import record_run_result
from cw.adapters.result import CodexResult
from cw.agents.reviewer import reviewer_prompt, run_review
from cw.checks.verification import (
    VerificationExecutor,
    _cleanup_runtime,
    _git_metadata_snapshot,
    _safe_runtime,
    doctor_verification_runtime,
    private_runtime_directory,
    validate_verification_receipt,
)
from cw.cli.main import main
from cw.core.errors import CwError, ErrorCode
from cw.core.models import RequiredCommand
from cw.core.review_infrastructure_recovery import (
    apply_review_infrastructure_recovery,
    authorize_legacy_retry,
    consume_legacy_authorization,
    pending_legacy_authorization,
    preview_review_infrastructure_recovery,
)
from cw.core.state import load_state
from cw.core.utils import atomic_json, sha256_file
from cw.core.workflow import workflow_hash
from tests.helpers import FakeAdapter, TempRepo, result

FIXTURE = Path(__file__).parent / "fixtures/reviewer-infrastructure-sanitized.json"
ROOT = Path(__file__).parents[1]


class VerificationExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.repo.artifact()
        self.repo.ready()
        phase = replace(
            self.repo.workflow.phases[0],
            required_commands=(RequiredCommand("python3 -c 'print(42)'", 20),),
        )
        self.workflow = replace(
            self.repo.workflow, phases=(phase, *self.repo.workflow.phases[1:])
        )
        self.phase = phase

    def tearDown(self) -> None:
        self.repo.close()

    def test_deterministic_execution_precedes_reviewer_and_receipt_is_canonical(
        self,
    ) -> None:
        report = run_review(
            self.repo.root,
            self.workflow,
            self.phase,
            self.repo.state(),
            FakeAdapter(result()),
        )
        receipt_ref = report["validation_evidence"]["verification_receipt"]
        receipt = validate_verification_receipt(
            self.repo.root,
            self.workflow,
            self.phase,
            receipt_ref["reference"],
            receipt_ref["sha256"],
        )
        self.assertEqual("cw.verification-receipt.v1", receipt["schema"])
        self.assertEqual(["python3", "-c", "print(42)"], receipt["commands"][0]["argv"])
        self.assertEqual(0, receipt["commands"][0]["exit_code"])
        serialized = json.dumps(receipt)
        self.assertNotIn(str(self.repo.root), serialized)
        self.assertNotIn(str(Path.home()), serialized)

    def test_receipt_tampering_cross_phase_workflow_revision_and_order_fail_closed(
        self,
    ) -> None:
        validation = VerificationExecutor().execute(
            self.repo.root, self.workflow, self.phase
        )
        self.assertTrue(validation.passed, validation.errors)
        reference = validation.receipt["reference"]
        path = self.repo.root / reference
        original = path.read_bytes()
        mutations = (
            ("phase_id", "02-phase-2"),
            ("workflow_id", "other-workflow"),
            ("plan_revision_id", "pr-" + "0" * 64),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                payload = json.loads(original)
                payload[key] = value
                atomic_json(path, payload)
                with self.assertRaises(CwError):
                    validate_verification_receipt(
                        self.repo.root,
                        self.workflow,
                        self.phase,
                        reference,
                        sha256_file(path),
                    )
                path.write_bytes(original)
        payload = json.loads(original)
        payload["commands"].append(dict(payload["commands"][0]))
        atomic_json(path, payload)
        with self.assertRaises(CwError):
            validate_verification_receipt(
                self.repo.root, self.workflow, self.phase, reference, sha256_file(path)
            )

    def test_artifact_change_after_receipt_is_rejected(self) -> None:
        validation = VerificationExecutor().execute(
            self.repo.root, self.workflow, self.phase
        )
        self.repo.artifact(content="tampered\n")
        with self.assertRaises(CwError) as raised:
            validate_verification_receipt(
                self.repo.root,
                self.workflow,
                self.phase,
                validation.receipt["reference"],
                validation.receipt["sha256"],
            )
        self.assertEqual(ErrorCode.INTEGRITY_ERROR, raised.exception.code)

    def test_runtime_environment_is_private_redacted_and_removed(self) -> None:
        captured: dict[str, str] = {}
        real_popen = __import__("subprocess").Popen

        def observe(*args, **kwargs):
            if "env" in kwargs and "TMPDIR" in kwargs["env"]:
                captured.update(
                    {
                        name: kwargs["env"][name]
                        for name in (
                            "TMPDIR",
                            "TMP",
                            "TEMP",
                            "XDG_CACHE_HOME",
                            "COMPOSER_CACHE_DIR",
                        )
                    }
                )
            return real_popen(*args, **kwargs)

        with patch("cw.checks.verification.subprocess.Popen", side_effect=observe):
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertTrue(validation.passed, validation.errors)
        self.assertTrue(captured)
        self.assertTrue(all(not Path(value).exists() for value in captured.values()))
        serialized = json.dumps(validation.receipt)
        self.assertTrue(all(value not in serialized for value in captured.values()))

    def test_timeout_and_process_infrastructure_are_structured(self) -> None:
        import subprocess

        timed_out = Mock()
        timed_out.communicate.side_effect = (
            subprocess.TimeoutExpired(["python3"], 1),
            ("", ""),
        )
        with patch("cw.checks.verification._git_metadata_snapshot", return_value={}), patch(
            "cw.checks.verification.subprocess.Popen", return_value=timed_out
        ), patch("cw.checks.verification.stop_process_group"):
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.VERIFICATION_TIMEOUT.value, validation.error_code)

        with patch("cw.checks.verification._git_metadata_snapshot", return_value={}), patch(
            "cw.checks.verification.subprocess.Popen",
            side_effect=OSError("permission denied"),
        ):
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertFalse(validation.passed)
        self.assertEqual(
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR.value, validation.error_code
        )

    def test_symlink_and_unwritable_runtime_fail_before_command(self) -> None:
        runtime = self.repo.root / ".cw/runtime/verification"
        runtime.symlink_to(self.repo.root / "docs", target_is_directory=True)
        with patch("cw.checks.verification._git_metadata_snapshot", return_value={}), patch(
            "cw.checks.verification.subprocess.Popen"
        ) as command:
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR.value, validation.error_code)
        command.assert_not_called()
        runtime.unlink()
        real_open = os.open

        def reject_probe(path, *args, **kwargs):
            if Path(path).name == "preflight.tmp":
                raise PermissionError("not writable")
            return real_open(path, *args, **kwargs)

        with patch("cw.checks.verification._git_metadata_snapshot", return_value={}), patch(
            "cw.checks.verification.os.open", side_effect=reject_probe
        ), patch("cw.checks.verification.subprocess.Popen") as command:
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR.value, validation.error_code)
        command.assert_not_called()

    def test_traversal_is_rejected_and_project_mutation_is_detected(self) -> None:
        traversal = replace(
            self.phase,
            required_commands=(RequiredCommand("python3 ../outside.py", 20),),
        )
        workflow = replace(
            self.workflow,
            phases=(traversal, *self.workflow.phases[1:]),
        )
        validation = VerificationExecutor().execute(self.repo.root, workflow, traversal)
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR.value, validation.error_code)

        mutation = replace(
            self.phase,
            required_commands=(
                RequiredCommand("python3 -c 'open(\"generated.txt\", \"w\").write(\"x\")'", 20),
            ),
        )
        workflow = replace(self.workflow, phases=(mutation, *self.workflow.phases[1:]))
        validation = VerificationExecutor().execute(self.repo.root, workflow, mutation)
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.VERIFICATION_COMMAND_FAILED.value, validation.error_code)

    def test_concurrent_run_recorder_metadata_is_not_project_mutation(self) -> None:
        phase = replace(
            self.phase,
            required_commands=(RequiredCommand("python3 -c 'print(42)'", 20),),
        )
        workflow = replace(self.workflow, phases=(phase, *self.workflow.phases[1:]))
        real_popen = subprocess.Popen

        def record_then_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
            run_log = self.repo.root / ".cw/logs/runs/active.jsonl"
            run_log.parent.mkdir(parents=True, exist_ok=True)
            run_log.write_text("event\n", encoding="utf-8")
            (self.repo.root / ".cw/runtime/active-run.json").write_text(
                "{}", encoding="utf-8",
            )
            return real_popen(*args, **kwargs)

        with patch("cw.checks.verification.subprocess.Popen", side_effect=record_then_spawn):
            validation = VerificationExecutor().execute(self.repo.root, workflow, phase)

        self.assertTrue(validation.passed, validation.errors)

    def test_git_metadata_snapshot_is_root_bound_not_cwd_or_environment(self) -> None:
        self.repo.artifact()
        self.repo.ready()
        readiness = self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        readiness.unlink()
        phase = replace(
            self.phase,
            required_commands=(
                RequiredCommand(
                    "python3 -c 'open(\".git/external-admin\", \"w\").write(\"x\")'",
                    20,
                ),
            ),
        )
        workflow = replace(self.workflow, phases=(phase, *self.workflow.phases[1:]))
        before_attempts = (self.repo.state()["attempt"], self.repo.state()["revision_attempt"])
        with tempfile.TemporaryDirectory(prefix="cw-unrelated-git-") as temporary:
            unrelated = Path(temporary) / "A unrelated"
            unrelated.mkdir()
            subprocess.run(["git", "init", "-q", str(unrelated)], check=True)
            previous = Path.cwd()
            try:
                os.chdir(unrelated)
                with patch.dict(
                    os.environ,
                    {
                        "GIT_DIR": str(unrelated / ".git"),
                        "GIT_WORK_TREE": str(unrelated),
                        "GIT_COMMON_DIR": str(unrelated / ".git"),
                        "GIT_INDEX_FILE": str(unrelated / ".git/index"),
                        "GIT_OBJECT_DIRECTORY": str(unrelated / ".git/objects"),
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(unrelated / ".git/objects"),
                    },
                ):
                    validation = VerificationExecutor().execute(
                        self.repo.root, workflow, phase
                    )
            finally:
                os.chdir(previous)
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.INTEGRITY_ERROR.value, validation.error_code)
        self.assertTrue((self.repo.root / ".git/external-admin").is_file())
        self.assertFalse(readiness.exists())
        self.assertEqual(before_attempts, (self.repo.state()["attempt"], self.repo.state()["revision_attempt"]))
        self.assertEqual([], list((self.repo.root / ".cw/verification-receipts").glob("*.json")))

    def test_git_metadata_snapshot_accepts_other_cwd_and_linked_worktree(self) -> None:
        self.repo.artifact()
        phase = replace(
            self.phase,
            required_commands=(RequiredCommand("python3 -c 'print(42)'", 20),),
        )
        workflow = replace(self.workflow, phases=(phase, *self.workflow.phases[1:]))
        with tempfile.TemporaryDirectory(prefix="cw-outside-git-") as temporary:
            outside = Path(temporary) / "outside"
            outside.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(outside)
                validation = VerificationExecutor().execute(self.repo.root, workflow, phase)
            finally:
                os.chdir(previous)
        self.assertTrue(validation.passed, validation.errors)

        with tempfile.TemporaryDirectory(prefix="cw-linked-git-") as temporary:
            main = Path(temporary) / "main"
            linked = Path(temporary) / "linked worktree ü"
            subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
            subprocess.run(["git", "-C", str(main), "config", "user.email", "cw@example.test"], check=True)
            subprocess.run(["git", "-C", str(main), "config", "user.name", "CW"], check=True)
            (main / "tracked.txt").write_text("tracked\n")
            subprocess.run(["git", "-C", str(main), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(main), "commit", "-qm", "fixture"], check=True)
            subprocess.run(
                ["git", "-C", str(main), "worktree", "add", "-q", "-b", "linked-fixture", str(linked)],
                check=True,
            )
            outside = Path(temporary) / "outside"
            outside.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(outside)
                before = _git_metadata_snapshot(linked)
                git_dir = Path(subprocess.run(["git", "-C", str(linked), "rev-parse", "--git-dir"], capture_output=True, text=True, check=True).stdout.strip())
                if not git_dir.is_absolute():
                    git_dir = linked / git_dir
                (git_dir / "external-admin").write_text("changed\n")
                self.assertNotEqual(before, _git_metadata_snapshot(linked))
                (git_dir / "external-admin").unlink()
                before = _git_metadata_snapshot(linked)
                common_dir = Path(subprocess.run(["git", "-C", str(linked), "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip())
                if not common_dir.is_absolute():
                    common_dir = linked / common_dir
                (common_dir / "external-common").write_text("changed\n")
                self.assertNotEqual(before, _git_metadata_snapshot(linked))
            finally:
                os.chdir(previous)

    def test_git_metadata_snapshot_rejects_incoherent_git_identity(self) -> None:
        with patch("cw.checks.verification.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["git"], 0, stdout="not-the-root\n", stderr=""
            )
            with self.assertRaises(CwError) as raised:
                _git_metadata_snapshot(self.repo.root)
        self.assertEqual(ErrorCode.INTEGRITY_ERROR, raised.exception.code)

    def test_git_preflight_failure_is_structured_and_never_escapes_as_internal(self) -> None:
        with patch(
            "cw.checks.verification._git_metadata_snapshot",
            side_effect=CwError("Git repository identity is incoherent", ErrorCode.INTEGRITY_ERROR),
        ):
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.INTEGRITY_ERROR.value, validation.error_code)
        self.assertEqual("Git repository identity is incoherent", validation.errors[0])
        diagnostic = validation.checks[-1]
        self.assertEqual("preflight", diagnostic["phase"])
        self.assertEqual("verification-executor", diagnostic["operation"])
        self.assertEqual("Run: cw validate", diagnostic["next_action"])
        self.assertNotIn(str(self.repo.root), json.dumps(diagnostic))

    def test_runtime_cleanup_retries_transient_permission_error(self) -> None:
        runtime = self.repo.root / ".cw/runtime/transient cleanup"
        runtime.mkdir()
        (runtime / "ü.txt").write_text("x", encoding="utf-8")
        actual = __import__("shutil").rmtree
        calls: list[float] = []
        attempts = 0

        def transient(path, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("locked")
            return actual(path, *args, **kwargs)

        with patch("cw.checks.verification.shutil.rmtree", side_effect=transient):
            _cleanup_runtime(runtime, sleeper=calls.append)
        self.assertFalse(runtime.exists())
        self.assertEqual(2, attempts)
        self.assertEqual([0.02], calls)

    def test_runtime_cleanup_persistent_permission_error_is_classified_before_receipt(self) -> None:
        runtime_error = CwError(
            "Verification runtime cleanup failed",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
            "Run: cw retry",
        )
        with patch("cw.checks.verification._cleanup_runtime", side_effect=runtime_error):
            validation = VerificationExecutor().execute(
                self.repo.root, self.workflow, self.phase
            )
        self.assertFalse(validation.passed)
        self.assertEqual(ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR.value, validation.error_code)
        self.assertIsNone(validation.receipt)
        self.assertEqual([], list((self.repo.root / ".cw/verification-receipts").glob("*.json")))
        diagnostic = validation.checks[-1]
        self.assertEqual("runtime_cleanup", diagnostic["phase"])
        self.assertEqual("runtime_cleanup", diagnostic["operation"])
        self.assertNotIn(str(self.repo.root), json.dumps(diagnostic))

    def test_private_runtime_cleanup_retries_and_restores_namespace(self) -> None:
        actual = __import__("shutil").rmtree
        calls = 0

        def transient(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("simulated Windows sharing violation")
            return actual(path, *args, **kwargs)

        with patch("cw.checks.verification.shutil.rmtree", side_effect=transient), private_runtime_directory(
            self.repo.root, "reviewer"
        ) as runtime:
            (runtime / "result.json").write_text("{}", encoding="utf-8")

        self.assertEqual(2, calls)
        self.assertFalse(any((self.repo.root / ".cw/runtime").rglob("cw-reviewer-*")))

    def test_private_runtime_restores_timestamps_when_windows_lacks_no_follow_utime(self) -> None:
        actual = os.utime

        def windows_utime(path, *args, **kwargs):
            if kwargs.get("follow_symlinks") is False:
                raise NotImplementedError("Windows no-follow utime unavailable")
            return actual(path, *args, **kwargs)

        with patch("cw.checks.verification.os.utime", side_effect=windows_utime), private_runtime_directory(
            self.repo.root, "reviewer"
        ) as runtime:
            (runtime / "result.json").write_text("{}", encoding="utf-8")

        self.assertFalse(any((self.repo.root / ".cw/runtime").rglob("cw-reviewer-*")))

    def test_runtime_preflight_completes_a_partial_windows_write(self) -> None:
        actual_write = os.write
        writes = 0

        def partial_write(descriptor: int, payload: bytes) -> int:
            nonlocal writes
            writes += 1
            if writes == 1:
                return actual_write(descriptor, payload[:5])
            return actual_write(descriptor, payload)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("cw.checks.verification.os.write", side_effect=partial_write),
        ):
            _safe_runtime(Path(temporary))

        self.assertGreaterEqual(writes, 2)

    def test_runtime_preflight_uses_binary_mode_when_windows_supports_it(self) -> None:
        binary_flag = 0x8000
        actual_open = os.open
        observed_flags: list[int] = []

        def windows_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int) -> int:
            observed_flags.append(flags)
            return actual_open(path, flags & ~binary_flag, mode)

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            os, "O_BINARY", binary_flag, create=True
        ), patch("cw.checks.verification.os.open", side_effect=windows_open):
            _safe_runtime(Path(temporary))

        self.assertEqual(1, len(observed_flags))
        self.assertNotEqual(0, observed_flags[0] & binary_flag)

    def test_private_runtime_cleanup_failure_is_classified_and_chains_primary(self) -> None:
        primary = CwError("reviewer output invalid", ErrorCode.REVIEWER_INVALID_OUTPUT)
        cleanup = CwError(
            "Verification runtime cleanup failed",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
            "Run: cw retry",
            details="[redacted]",
        )
        with patch("cw.checks.verification._cleanup_runtime", side_effect=cleanup), self.assertRaises(
            CwError
        ) as raised, private_runtime_directory(self.repo.root, "reviewer"):
            raise primary

        self.assertIs(cleanup, raised.exception)
        self.assertEqual(ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR, raised.exception.code)
        self.assertIs(primary, raised.exception.__cause__)

    def test_governed_runtime_context_uses_only_the_canonical_cleanup_primitive(self) -> None:
        import inspect

        source = inspect.getsource(private_runtime_directory)
        self.assertIn("_cleanup_runtime(runtime)", source)
        self.assertNotIn("shutil.rmtree", source)

    def test_git_snapshot_requests_absolute_paths_from_the_bound_root(self) -> None:
        observed: list[list[str]] = []
        actual = subprocess.run

        def capture(command, *args, **kwargs):
            observed.append(command)
            return actual(command, *args, **kwargs)

        with patch("cw.checks.verification.subprocess.run", side_effect=capture):
            _git_metadata_snapshot(self.repo.root)
        self.assertEqual(3, len(observed))
        for command in observed:
            self.assertEqual("git", command[0])
            self.assertEqual("-C", command[1])
            self.assertEqual(str(self.repo.root.resolve()), command[2])
            self.assertEqual("rev-parse", command[3])
            self.assertEqual("--path-format=absolute", command[4])

    def test_duplicate_and_hardlinked_receipts_are_rejected(self) -> None:
        validation = VerificationExecutor().execute(
            self.repo.root, self.workflow, self.phase
        )
        reference = validation.receipt["reference"]
        path = self.repo.root / reference
        duplicate = path.with_name("duplicate.json")
        duplicate.write_bytes(path.read_bytes())
        with self.assertRaises(CwError):
            validate_verification_receipt(
                self.repo.root, self.workflow, self.phase, reference, sha256_file(path)
            )
        duplicate.unlink()
        hardlink = path.with_name("hardlink.json")
        os.link(path, hardlink)
        with self.assertRaises(CwError):
            validate_verification_receipt(
                self.repo.root, self.workflow, self.phase, reference, sha256_file(path)
            )

    def test_doctor_preflight_is_read_only_and_reports_all_dimensions(self) -> None:
        before = {
            path.relative_to(self.repo.root).as_posix(): (
                path.lstat().st_mode,
                path.lstat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.repo.root.rglob("*")
        }
        checks = doctor_verification_runtime(self.repo.root)
        self.assertTrue(all(item["status"] == "pass" for item in checks), checks)
        self.assertEqual(
            before,
            {
                path.relative_to(self.repo.root).as_posix(): (
                    path.lstat().st_mode,
                    path.lstat().st_mtime_ns,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in self.repo.root.rglob("*")
            },
        )


class SemanticReviewerIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.repo.artifact()
        self.repo.ready()

    def tearDown(self) -> None:
        self.repo.close()

    def test_prompt_treats_repository_and_artifacts_as_untrusted(self) -> None:
        prompt = reviewer_prompt(
            self.repo.workflow,
            self.repo.workflow.phases[0],
            {"receipt_sha256": "sha256:" + "0" * 64},
        )
        for required in (
            "untrusted data",
            "NEVER execute project commands",
            "prompt",
            "Verification Receipt",
            "must not be represented as semantic REVISE",
        ):
            self.assertIn(required, prompt)

    def test_sanitized_fixture_records_the_017_failure_without_consumer_data(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertFalse(fixture["consumer_data"])
        self.assertFalse(fixture["external_services"])
        self.assertEqual("REVIEWER_INFRASTRUCTURE_ERROR", fixture["v0_18_expected"]["classification"])
        self.assertIn("valid REVISE payload is accepted", fixture["v0_17_reproduction"])
        self.assertNotIn("/home/", FIXTURE.read_text(encoding="utf-8"))

    def test_reviewer_command_event_discards_valid_revise_result(self) -> None:
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "command_execution",
                            "command": "composer test",
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        )
        self.assertTrue(CodexAdapter._reviewer_executed_command(stdout))
        self.assertFalse(
            CodexAdapter._reviewer_executed_command(
                json.dumps(
                    {
                        "type": "item.completed",
                        "decision": "APPROVE",
                        "item": {"type": "reasoning"},
                    }
                )
            )
        )
        schema = ROOT / "cw/schemas/codex/review-output.schema.json"
        with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch.object(
            CodexAdapter,
            "_run_streaming",
            return_value=CodexResult(
                result(decision="REVISE", status="FAIL"), "", stdout
            ),
        ), self.assertRaises(CwError) as raised:
            CodexAdapter().run_reviewer(self.repo.root, "review", schema, 10)
        self.assertEqual(
            ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR, raised.exception.code
        )

    def test_reviewer_infrastructure_preserves_both_attempt_counters_and_readiness(
        self,
    ) -> None:
        before = (self.repo.state()["attempt"], self.repo.state()["revision_attempt"])
        error = CwError(
            "reviewer command attempted", ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR
        )
        with self.assertRaises(CwError):
            run_review(
                self.repo.root,
                self.repo.workflow,
                self.repo.workflow.phases[0],
                self.repo.state(),
                FakeAdapter(error=error),
            )
        after = self.repo.state()
        self.assertEqual(before, (after["attempt"], after["revision_attempt"]))
        self.assertTrue(
            (self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json").is_file()
        )
        self.assertEqual("ERROR", after["status"])

    def test_retry_regenerates_verification_without_rerunning_implementer(self) -> None:
        with self.assertRaises(CwError):
            run_review(
                self.repo.root,
                self.repo.workflow,
                self.repo.workflow.phases[0],
                self.repo.state(),
                FakeAdapter(
                    error=CwError(
                        "reviewer sandbox unavailable",
                        ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR,
                    )
                ),
            )
        readiness = json.loads(
            (self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json").read_text()
        )
        original_receipt = readiness["verification_receipt"]["reference"]
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.repo.root)
            with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
                "cw.agents.reviewer.CodexAdapter.run_reviewer",
                return_value=CodexResult(result(), ""),
            ), redirect_stdout(output):
                code = main(("retry", "--json"))
        finally:
            os.chdir(previous)
        self.assertEqual(0, code, output.getvalue())
        implementer.assert_not_called()
        review = json.loads((self.repo.root / self.repo.state()["last_review"]).read_text())
        self.assertNotEqual(
            original_receipt,
            review["validation_evidence"]["verification_receipt"]["reference"],
        )

    def test_semantic_revise_consumes_exactly_one_attempt_and_has_no_retry(self) -> None:
        run_review(
            self.repo.root,
            self.repo.workflow,
            self.repo.workflow.phases[0],
            self.repo.state(),
            FakeAdapter(result(decision="REVISE", status="FAIL")),
        )
        state = self.repo.state()
        self.assertEqual((1, 1), (state["attempt"], state["revision_attempt"]))
        self.assertEqual("REVISION_REQUIRED", state["status"])
        self.assertIsNone(state.get("infrastructure_error"))


class HistoricalInfrastructureRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.repo.artifact()
        self.repo.ready()
        payload = result(decision="REVISE", status="FAIL")
        payload["summary"] = (
            "Reviewer attempted composer but its cache was not writable: permission denied"
        )
        run_review(
            self.repo.root,
            self.repo.workflow,
            self.repo.workflow.phases[0],
            self.repo.state(),
            FakeAdapter(payload),
        )
        record_run_result(
            self.repo.root,
            "reviewer",
            exit_code=0,
            stdout=json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "composer test",
                        "exit_code": 1,
                    },
                }
            ),
            stderr="cache directory permission denied",
            diagnostics=(),
        )
        self.review = self.repo.state()["last_review"]
        self.review_sha = sha256_file(self.repo.root / self.review)
        self.workflow_sha = workflow_hash(
            self.repo.root / ".codex/workflow/phases.yaml"
        )
        self.state_sha = sha256_file(self.repo.root / ".cw/state.json")
        self.args = (
            "01-phase-1",
            self.review,
            self.review_sha,
            self.workflow_sha,
            self.state_sha,
            "Reviewer command execution failed only because its private cache was unavailable",
        )

    def tearDown(self) -> None:
        self.repo.close()

    def invoke(self, *arguments: str) -> tuple[int, str]:
        previous = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.repo.root)
            with redirect_stdout(output):
                code = main(arguments)
        finally:
            os.chdir(previous)
        return code, output.getvalue()

    def authorize(self, *, apply: bool = True) -> dict[str, object]:
        return authorize_legacy_retry(
            self.repo.root,
            *self.args,
            acknowledgement=True,
            apply=apply,
        )

    def test_unlinked_logs_fail_closed_and_authorization_preview_is_non_mutating(self) -> None:
        before = {
            path.relative_to(self.repo.root).as_posix(): path.read_bytes()
            for path in self.repo.root.rglob("*")
            if path.is_file()
        }
        with self.assertRaises(CwError) as raised:
            preview_review_infrastructure_recovery(self.repo.root, *self.args)
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)
        preview = self.authorize(apply=False)
        self.assertFalse(preview["changed"])
        self.assertEqual("PENDING", preview["authorization_status"])
        self.assertEqual(
            before,
            {
                path.relative_to(self.repo.root).as_posix(): path.read_bytes()
                for path in self.repo.root.rglob("*")
                if path.is_file()
            },
        )
        applied = self.authorize()
        state = load_state(self.repo.root)
        self.assertTrue(applied["changed"])
        self.assertEqual(1, state["attempt"])
        self.assertEqual(1, state["revision_attempt"])
        self.assertEqual("REVISION_REQUIRED", state["status"])
        self.assertTrue((self.repo.root / self.review).is_file())
        replay = self.authorize()
        self.assertFalse(replay["changed"])
        self.assertTrue(replay["idempotent_replay"])

    def test_wrong_review_digest_cas_semantic_review_and_missing_proof_fail_closed(
        self,
    ) -> None:
        cases = [
            (*self.args[:2], "sha256:" + "0" * 64, *self.args[3:]),
            (*self.args[:3], "sha256:" + "0" * 64, *self.args[4:]),
            (*self.args[:4], "sha256:" + "0" * 64, *self.args[5:]),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments[2]), self.assertRaises(CwError):
                preview_review_infrastructure_recovery(self.repo.root, *arguments)
        (self.repo.root / ".cw/logs/codex-runs.jsonl").unlink()
        with self.assertRaises(CwError):
            preview_review_infrastructure_recovery(self.repo.root, *self.args)

    def test_session_lock_journal_and_current_gate_fail_closed(self) -> None:
        session = self.repo.root / ".cw/runtime/implementer-session.json"
        atomic_json(session, {"synthetic": True})
        with self.assertRaises(CwError) as raised:
            preview_review_infrastructure_recovery(self.repo.root, *self.args)
        self.assertEqual(ErrorCode.LOCKED, raised.exception.code)
        session.unlink()

        lock = self.repo.root / ".cw/locks/operation.lock"
        atomic_json(lock, {"pid": 1})
        with self.assertRaises(CwError) as raised:
            preview_review_infrastructure_recovery(self.repo.root, *self.args)
        self.assertEqual(ErrorCode.LOCKED, raised.exception.code)
        lock.unlink()

        journal = self.repo.root / ".cw/runtime/review-infrastructure-recovery.json"
        atomic_json(journal, {"kind": "synthetic-pending"})
        with self.assertRaises(CwError) as raised:
            preview_review_infrastructure_recovery(self.repo.root, *self.args)
        self.assertEqual(ErrorCode.TRANSACTION_RECOVERY_REQUIRED, raised.exception.code)
        journal.unlink()

        gate = self.repo.root / ".cw/gates/01-phase-1.approved.json"
        atomic_json(gate, {"synthetic": True})
        with self.assertRaises(CwError) as raised:
            preview_review_infrastructure_recovery(self.repo.root, *self.args)
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)

    def test_failure_boundaries_restore_state_and_evidence(self) -> None:
        for boundary in (
            "prepared",
            "backup_ready",
            "supersession",
            "state",
            "receipt",
        ):
            with self.subTest(boundary=boundary):

                def fail(name: str, selected: str = boundary) -> None:
                    if name == selected:
                        raise RuntimeError(selected)

                with self.assertRaises(RuntimeError):
                    apply_review_infrastructure_recovery(
                        self.repo.root, *self.args, failure_injector=fail
                    )
                self.assertEqual(
                    "REVISION_REQUIRED", load_state(self.repo.root)["status"]
                )
                self.assertFalse(
                    (
                        self.repo.root
                        / ".cw/runtime/review-infrastructure-recovery.json"
                    ).exists()
                )
                self.assertEqual(
                    [],
                    list(
                        (self.repo.root / ".cw/review-infrastructure-recoveries").glob(
                            "*.json"
                        )
                    )
                    if (
                        self.repo.root / ".cw/review-infrastructure-recoveries"
                    ).exists()
                    else [],
                )

    def test_exact_authorization_replay_is_idempotent_and_different_request_conflicts(self) -> None:
        applied = self.authorize()
        self.assertTrue(applied["changed"])
        replay = self.authorize()
        self.assertTrue(replay["idempotent_replay"])
        with self.assertRaises(CwError) as raised:
            authorize_legacy_retry(
                self.repo.root,
                *self.args[:-1],
                "a different authorization reason",
                acknowledgement=True,
                apply=True,
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_technical_error_preserves_authorization_and_revise_consumes_it_once(self) -> None:
        authorization = self.authorize()
        path = self.repo.root / ".cw/review-retry-authorizations" / f"{authorization['authorization_id']}.json"
        original_review = (self.repo.root / self.review).read_bytes()
        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
            side_effect=CwError("reviewer transport failed", ErrorCode.REVIEWER_NETWORK_ERROR),
        ):
            code, _ = self.invoke("retry", "--json")
        self.assertEqual(1, code)
        implementer.assert_not_called()
        self.assertEqual("PENDING", json.loads(path.read_text())["status"])
        self.assertEqual((1, 1), (self.repo.state()["attempt"], self.repo.state()["revision_attempt"]))
        readiness = json.loads(
            (self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json").read_text()
        )
        historical_receipt = json.loads(
            (self.repo.root / self.review).read_text()
        )["validation_evidence"]["verification_receipt"]["reference"]
        self.assertNotEqual(
            historical_receipt, readiness["verification_receipt"]["reference"]
        )
        self.assertIsNotNone(pending_legacy_authorization(self.repo.root, self.repo.state()))

        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
            return_value=CodexResult(result(decision="REVISE", status="FAIL"), ""),
        ):
            code, _ = self.invoke("retry", "--json")
        self.assertEqual(1, code)
        implementer.assert_not_called()
        self.assertEqual("CONSUMED", json.loads(path.read_text())["status"])
        self.assertEqual((1, 1), (self.repo.state()["attempt"], self.repo.state()["revision_attempt"]))
        self.assertEqual(original_review, (self.repo.root / self.review).read_bytes())
        with self.assertRaises(CwError) as raised:
            consume_legacy_authorization(
                self.repo.root,
                str(authorization["authorization_id"]),
                self.repo.state()["last_review"],
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_authorization_preserves_historical_review(self) -> None:
        from cw.core.audit import audit_history

        original = (self.repo.root / self.review).read_bytes()
        self.authorize()
        result_ = audit_history(
            self.repo.root, self.repo.workflow, load_state(self.repo.root)
        )
        self.assertGreaterEqual(result_["reviews"], 1)
        self.assertEqual(original, (self.repo.root / self.review).read_bytes())

    def test_cli_authorization_preview_projects_human_json_jsonl_llm_and_fields_without_mutation(self) -> None:
        base = (
            "review", "authorize-retry", "--phase", self.args[0],
            "--review-ref", self.args[1], "--expected-review-sha256", self.args[2],
            "--expected-workflow-sha256", self.args[3], "--expected-state-sha256", self.args[4],
            "--reason", self.args[5], "--acknowledge-unverifiable-legacy", "--dry-run",
        )
        state_before = (self.repo.root / ".cw/state.json").read_bytes()
        for mode in ((), ("--output=json",), ("--output=jsonl",), ("--llm",)):
            code, output = self.invoke(*base, *mode)
            self.assertEqual(0, code, output)
            self.assertIn(
                "HUMAN_AUTHORIZED_LEGACY_REVIEW_RETRY"
                if not mode
                else "AUTHORIZATION_PREVIEW",
                output,
            )
        code, output = self.invoke(*base, "--output=json", "--fields", "result,changed,next_action")
        self.assertEqual(0, code, output)
        envelope = json.loads(output)
        self.assertFalse(envelope["changed"])
        self.assertEqual(
            {"result", "changed", "next_action"},
            set(envelope["data"]),
        )
        self.assertEqual(state_before, (self.repo.root / ".cw/state.json").read_bytes())

    def test_authorized_retry_regenerates_verification_without_implementer_and_approve_consumes_once(self) -> None:
        authorization = self.authorize()
        original_receipt = json.loads((self.repo.root / self.review).read_text())["validation_evidence"]["verification_receipt"]["reference"]
        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer, patch(
            "cw.agents.reviewer.CodexAdapter.run_reviewer",
            return_value=CodexResult(result(), ""),
        ) as reviewer:
            code, output = self.invoke("retry", "--json")
        self.assertEqual(0, code, output)
        implementer.assert_not_called()
        reviewer.assert_called_once()
        final = load_state(self.repo.root)
        self.assertEqual("IN_PROGRESS", final["status"])
        self.assertEqual("02-phase-2", final["current_phase"])
        self.assertTrue((self.repo.root / ".cw/gates/01-phase-1.approved.json").is_file())
        authorization_path = self.repo.root / ".cw/review-retry-authorizations" / f"{authorization['authorization_id']}.json"
        self.assertEqual("CONSUMED", json.loads(authorization_path.read_text())["status"])
        renewed = json.loads((self.repo.root / final["last_review"]).read_text())
        self.assertNotEqual(
            original_receipt,
            renewed["validation_evidence"]["verification_receipt"]["reference"],
        )


class PublicReviewerInfrastructureContractTests(unittest.TestCase):
    """Keep public contract coverage runnable by the canonical unittest suite."""

    def test_public_reviewer_infrastructure_contracts(self) -> None:
        requirements = [
        ("core-version", "VERSION", "0.18.0"),
        ("plugin-version", "plugins/cw/VERSION", "0.1.0"),
        ("remote-protocol", "cw/remote/protocol.py", "cw.remote.v1"),
        ("output-schema", "cw/output_protocol.py", "cw.output.v1"),
        ("project-schema", "cw/core/schema.py", "SCHEMA_VERSION = 1"),
        ("governance-schema", "cw/core/governance.py", '"schema_version": 2'),
        ("executor-class", "cw/checks/verification.py", "class VerificationExecutor"),
        ("executor-shell", "cw/checks/verification.py", "shell=False"),
        ("executor-stdin", "cw/checks/verification.py", "stdin=subprocess.DEVNULL"),
        ("executor-tmpdir", "cw/checks/verification.py", '"TMPDIR"'),
        ("executor-composer-cache", "cw/checks/verification.py", '"COMPOSER_CACHE_DIR"'),
        ("executor-ruff-cache", "cw/checks/verification.py", '"RUFF_CACHE_DIR"'),
        ("receipt-schema", "cw/schemas/verification-receipt.schema.json", "cw.verification-receipt.v1"),
        ("receipt-workflow", "cw/schemas/verification-receipt.schema.json", "workflow_sha256"),
        ("receipt-state", "cw/schemas/verification-receipt.schema.json", "state_sha256_before"),
        ("receipt-contract", "cw/schemas/verification-receipt.schema.json", "completion_contract_sha256"),
        ("receipt-artifacts", "cw/schemas/verification-receipt.schema.json", "artifact_identities"),
        ("receipt-redacted-stdout", "cw/schemas/verification-receipt.schema.json", "stdout_sha256"),
        ("reviewer-untrusted", "cw/agents/reviewer.py", "untrusted data"),
        ("reviewer-no-command", "cw/agents/reviewer.py", "NEVER execute project commands"),
        ("reviewer-no-install", "cw/agents/reviewer.py", "NEVER install dependencies"),
        ("reviewer-receipt", "cw/agents/reviewer.py", "Verification Receipt is authoritative"),
        ("reviewer-no-semantic-infra", "cw/agents/reviewer.py", "must not be represented as semantic REVISE"),
        ("reviewer-read-only", "cw/adapters/codex.py", '"--sandbox", "read-only"'),
        ("reviewer-hooks", "cw/adapters/codex.py", '"--disable", "hooks"'),
        ("reviewer-web", "cw/adapters/codex.py", 'web_search="disabled"'),
        ("reviewer-command-event", "cw/adapters/codex.py", "_reviewer_executed_command"),
        ("retry-verification", "cw/core/recovery.py", 'ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR: "verification"'),
        ("retry-reviewer", "cw/core/recovery.py", 'ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR: "review"'),
        ("explain-retry", "cw/application/status.py", '"cw retry" if retryable'),
        ("recovery-preview", "cw/core/review_infrastructure_recovery.py", "preview_review_infrastructure_recovery"),
        ("recovery-apply", "cw/core/review_infrastructure_recovery.py", "apply_review_infrastructure_recovery"),
        ("recovery-backup", "cw/core/review_infrastructure_recovery.py", '"status": "BACKUP_READY"'),
        ("recovery-committed", "cw/core/review_infrastructure_recovery.py", '"status": "COMMITTED"'),
        ("recovery-no-readiness", "cw/core/review_infrastructure_recovery.py", '"readiness_available": False'),
        ("recovery-next-action", "cw/core/review_infrastructure_recovery.py", '"next_action": "cw retry --json"'),
        ("cli-recovery", "cw/cli/parser.py", '"recover-infrastructure"'),
        ("docs-retry", "docs/reviewer-infrastructure.md", "cw retry"),
        ("docs-dry-run", "docs/reviewer-infrastructure.md", "--dry-run"),
        ("docs-apply", "docs/reviewer-infrastructure.md", "--apply"),
        ("docs-no-auto-approval", "docs/reviewer-infrastructure.md", "approve, create a gate"),
        ("changelog-operation", "CHANGELOG.md", "recover-infrastructure"),
        ]
        for requirement, relative, token in requirements:
            with self.subTest(requirement=requirement, path=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(token, content, requirement)


if __name__ == "__main__":
    unittest.main()
