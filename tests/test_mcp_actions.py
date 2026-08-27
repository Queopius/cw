from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cw.adapters.mcp import MCPRuntime, RuntimeConfig
from cw.agents.reviewer import run_review
from cw.application import (
    Actor,
    ActorOrigin,
    ApplicationError,
    ApplicationErrorCode,
    CWApplication,
    OperationContext,
)
from cw.cli.main import main as cli_main
from cw.core.completion import run_completion_review
from cw.core.errors import CwError, ErrorCode
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.recovery import mark_infrastructure_error
from cw.core.state import save_state, transition
from cw.core.utils import sha256_file
from tests.helpers import FakeAdapter, TempRepo, result


def file_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class BlockingAdapter(FakeAdapter):
    def __init__(self, payload: dict) -> None:
        super().__init__(payload)
        self.started = threading.Event()
        self.release = threading.Event()

    def run_reviewer(self, root, prompt, schema, timeout):
        self.started.set()
        if not self.release.wait(10):
            raise CwError("fixture timed out", ErrorCode.REVIEW_TIMEOUT)
        return super().run_reviewer(root, prompt, schema, timeout)


class MCPControlledActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]),
            diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: FakeAdapter(result(1)),
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]

    def tearDown(self) -> None:
        self.runtime.shutdown()
        self.repo.close()

    def call(self, name: str, **arguments):
        return self.runtime.call_tool(name, {"project_id": self.handle, **arguments})

    def wait(self, operation_id: str, *, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        poll = 0
        while time.monotonic() < deadline:
            response = self.call(
                "cw_operation_status",
                operation_id=f"poll-{operation_id}-{poll}",
                target_operation_id=operation_id,
            )
            if response["status"] not in {"QUEUED", "RUNNING"}:
                return response
            poll += 1
            time.sleep(0.01)
        self.fail(f"operation did not finish: {operation_id}")

    def test_fake_codex_controlled_flow_creates_gate_only_through_supervisor(self) -> None:
        started = self.call("cw_phase_start", operation_id="phase-start-1")
        self.assertIn(started["status"], {"QUEUED", "RUNNING", "SUCCEEDED"})
        started = self.wait("phase-start-1")
        self.assertEqual("SUCCEEDED", started["status"])
        self.assertEqual("01-phase-1", started["data"]["result"]["phase"])

        self.repo.artifact(1)
        self.repo.ready(1)
        validation = self.call("cw_validate", operation_id="validation-1")
        self.assertIn(validation["status"], {"QUEUED", "RUNNING", "SUCCEEDED"})
        validation = self.wait("validation-1")
        self.assertEqual("PASSED", validation["data"]["result"]["validation_status"])

        review = self.call("cw_request_review", operation_id="review-1")
        self.assertIn(review["status"], {"QUEUED", "RUNNING", "SUCCEEDED"})
        review = self.wait("review-1")
        self.assertEqual("APPROVE", review["data"]["result"]["decision"])
        self.assertTrue((self.repo.root / ".cw/gates/01-phase-1.approved.json").is_file())
        self.assertEqual("02-phase-2", self.repo.state()["current_phase"])

    def test_idempotency_replay_and_payload_conflict_are_safe(self) -> None:
        first = self.call("cw_phase_start", operation_id="same-action")
        finished = self.wait("same-action")
        second = self.call("cw_phase_start", operation_id="same-action")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(finished["data"]["result"], second["data"]["result"])
        conflict = self.call("cw_validate", operation_id="same-action")
        self.assertEqual("OPERATION_CONFLICT", conflict["error"]["code"])
        sessions = list((self.repo.root / ".cw/runtime").glob("implementer-session.json"))
        self.assertEqual(1, len(sessions))
        self.assertIn(first["status"], {"QUEUED", "RUNNING", "SUCCEEDED"})

    def test_action_schema_rejects_phase_command_review_and_actor_injection(self) -> None:
        attempts = (
            ("cw_phase_start", {"phase": "99-attacker"}),
            ("cw_validate", {"command": "echo attacker"}),
            ("cw_request_review", {"decision": "APPROVE"}),
            ("cw_retry", {"actor_origin": "internal_supervisor"}),
        )
        for index, (name, injected) in enumerate(attempts):
            response = self.call(name, operation_id=f"injected-{index}", **injected)
            self.assertEqual("INVALID_REQUEST", response["error"]["code"])
        self.assertFalse((self.repo.root / ".cw/runtime/implementer-session.json").exists())

    def test_high_consequence_and_generic_capabilities_remain_absent(self) -> None:
        for name in (
            "cw_authorize_extension", "cw_repair", "cw_create_gate", "cw_execute",
            "shell", "git", "filesystem_write", "cw_rebaseline",
        ):
            response = self.call(name, operation_id=f"forbidden-{name}")
            self.assertEqual("AUTHORIZATION_REQUIRED", response["error"]["code"])

    def test_internal_planner_and_reviewer_cannot_request_controlled_actions(self) -> None:
        application = CWApplication(
            allowed_roots=[self.repo.root],
            review_backend_factory=lambda: FakeAdapter(result(1)),
        )
        project = application.open_project(self.repo.root)
        try:
            for origin in (ActorOrigin.PLANNER, ActorOrigin.REVIEWER, ActorOrigin.INTERNAL_SUPERVISOR):
                request = OperationContext(
                    f"forged-{origin.value}", Actor(f"forged-{origin.value}", origin), "phase.start",
                )
                with self.assertRaises(ApplicationError) as raised:
                    application.phase_start(project, request)
                self.assertEqual(ApplicationErrorCode.AUTHORIZATION_REQUIRED, raised.exception.code)
        finally:
            application.shutdown()

    def test_phase_start_refuses_existing_session_without_duplication(self) -> None:
        self.call("cw_phase_start", operation_id="start-a")
        self.assertEqual("SUCCEEDED", self.wait("start-a")["status"])
        self.call("cw_phase_start", operation_id="start-b")
        duplicate = self.wait("start-b")
        self.assertEqual("BLOCKED", duplicate["status"])
        self.assertEqual("OPERATION_CONFLICT", duplicate["data"]["error"]["code"])

    def test_validation_failure_is_semantic_result_not_infrastructure_failure(self) -> None:
        response = self.call("cw_validate", operation_id="validation-fails")
        response = self.wait("validation-fails")
        self.assertEqual("SUCCEEDED", response["status"])
        self.assertEqual("FAILED", response["data"]["result"]["validation_status"])
        self.assertTrue(response["data"]["result"]["errors"])

    def test_cli_and_mcp_validation_share_semantic_result(self) -> None:
        cli_repo = TempRepo(name="cli-parity", phases=1)
        previous = Path.cwd()
        try:
            self.repo.artifact(1)
            self.repo.ready(1)
            cli_repo.artifact(1)
            cli_repo.ready(1)
            output = io.StringIO()
            os.chdir(cli_repo.root)
            with redirect_stdout(output):
                code = cli_main(("validate", "--json"))
            self.assertEqual(0, code)
            cli_payload = json.loads(output.getvalue())
            os.chdir(previous)

            self.call("cw_validate", operation_id="validation-parity")
            mcp = self.wait("validation-parity")["data"]["result"]
            self.assertEqual(cli_payload["passed"], mcp["validation_status"] == "PASSED")
            self.assertEqual(
                [item["name"] for item in cli_payload["checks"]],
                [item["name"] for item in mcp["checks"]],
            )
            self.assertEqual(cli_payload["artifact_hashes"], mcp["artifact_hashes"])
        finally:
            os.chdir(previous)
            cli_repo.close()

    def test_completed_project_and_inconsistent_phase_cannot_start(self) -> None:
        for phase in (1, 2):
            self.repo.artifact(phase)
            self.repo.ready(phase)
            run_review(
                self.repo.root, self.repo.workflow, self.repo.workflow.phases[phase - 1],
                self.repo.state(), FakeAdapter(result(phase)),
            )
        self.call("cw_phase_start", operation_id="start-completed")
        completed = self.wait("start-completed")
        self.assertEqual("FAILED", completed["status"])
        self.assertEqual("PROJECT_COMPLETED", completed["data"]["error"]["code"])

        other = TempRepo(name="inconsistent")
        runtime = MCPRuntime(RuntimeConfig.create([other.root]), diagnostic_sink=lambda _: None)
        try:
            state = other.state()
            state["current_phase"] = "02-phase-2"
            save_state(other.root, state)
            handle = runtime.project_handles()[0]["repository_id"]
            runtime.call_tool("cw_phase_start", {
                "project_id": handle, "operation_id": "start-without-prior-gate",
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = runtime.call_tool("cw_operation_status", {
                    "project_id": handle, "operation_id": "poll-inconsistent",
                    "target_operation_id": "start-without-prior-gate",
                })
                if response["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
            self.assertEqual("STATE_INCONSISTENT", response["data"]["error"]["code"])
        finally:
            runtime.shutdown()
            other.close()

    def test_review_rejection_does_not_create_gate(self) -> None:
        self.runtime.shutdown()
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: FakeAdapter(result(1, "REVISE", "FAIL")),
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]
        self.repo.artifact(1)
        self.repo.ready(1)
        self.call("cw_request_review", operation_id="review-revise")
        response = self.wait("review-revise")
        self.assertEqual("SUCCEEDED", response["status"])
        self.assertEqual("REVISE", response["data"]["result"]["decision"])
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
        self.assertEqual("REVISION_REQUIRED", self.repo.state()["status"])

    def test_malformed_reviewer_output_is_blocked_without_gate(self) -> None:
        self.runtime.shutdown()
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: FakeAdapter({"decision": "APPROVE"}),
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]
        self.repo.artifact(1)
        self.repo.ready(1)
        self.call("cw_request_review", operation_id="review-malformed")
        response = self.wait("review-malformed")
        self.assertIn(response["status"], {"FAILED", "BLOCKED"})
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())

    def test_reviewer_infrastructure_failure_is_blocked_and_retryable(self) -> None:
        self.runtime.shutdown()
        failure = CwError("network unavailable", ErrorCode.REVIEWER_NETWORK_ERROR)
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: FakeAdapter(error=failure),
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]
        self.repo.artifact(1)
        self.repo.ready(1)
        self.call("cw_request_review", operation_id="review-infrastructure")
        response = self.wait("review-infrastructure")
        self.assertEqual("BLOCKED", response["status"])
        self.assertEqual("INFRASTRUCTURE_FAILURE", response["data"]["error"]["code"])
        self.assertTrue(response["data"]["error"]["retryable"])
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())

    def test_expected_mutation_sets_are_bounded(self) -> None:
        before = file_snapshot(self.repo.root / ".cw")
        self.call("cw_phase_start", operation_id="bounded-start")
        self.assertEqual("SUCCEEDED", self.wait("bounded-start")["status"])
        after = file_snapshot(self.repo.root / ".cw")
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertTrue(changed)
        self.assertTrue(all(
            name == "runtime/implementer-session.json" or name.startswith("runtime/operations/")
            for name in changed
        ), changed)

        self.repo.artifact(1)
        self.repo.ready(1)
        before = file_snapshot(self.repo.root / ".cw")
        self.call("cw_validate", operation_id="bounded-validation")
        self.assertEqual("SUCCEEDED", self.wait("bounded-validation")["status"])
        after = file_snapshot(self.repo.root / ".cw")
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertTrue(all(
            name.startswith(
                ("runtime/operations/", "validation/", "verification-receipts/")
            )
            or name == "runtime/READY_FOR_REVIEW.json"
            for name in changed
        ), changed)

    def test_review_and_retry_mutation_sets_are_bounded(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        before = file_snapshot(self.repo.root / ".cw")
        self.call("cw_request_review", operation_id="bounded-review")
        self.assertEqual("SUCCEEDED", self.wait("bounded-review")["status"])
        after = file_snapshot(self.repo.root / ".cw")
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertTrue(changed)
        self.assertTrue(all(
            name == "state.json"
            or name in {"runtime/implementer-session.json", "runtime/READY_FOR_REVIEW.json"}
            or name.startswith(
                (
                    "runtime/operations/",
                    "reviews/",
                    "gates/",
                    "verification-receipts/",
                )
            )
            for name in changed
        ), changed)

        retry_repo = TempRepo(name="bounded-retry", phases=1)
        runtime = MCPRuntime(RuntimeConfig.create([retry_repo.root]), diagnostic_sink=lambda _: None)
        try:
            state = retry_repo.state()
            error = CwError("implementer stopped", ErrorCode.IMPLEMENTER_PROCESS_ERROR)
            state["last_error"] = f"{error.code.value}: {error.message}"
            mark_infrastructure_error(state, error, operation="implementation", phase="01-phase-1")
            transition(retry_repo.root, state, WorkflowState.ERROR, force_error=True)
            before = file_snapshot(retry_repo.root / ".cw")
            handle = runtime.project_handles()[0]["repository_id"]
            runtime.call_tool("cw_retry", {
                "project_id": handle, "operation_id": "bounded-retry-operation",
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = runtime.call_tool("cw_operation_status", {
                    "project_id": handle, "operation_id": "bounded-retry-poll",
                    "target_operation_id": "bounded-retry-operation",
                })
                if response["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
            self.assertEqual("SUCCEEDED", response["status"])
            after = file_snapshot(retry_repo.root / ".cw")
            changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
            self.assertTrue(all(
                name == "state.json"
                or name == "runtime/implementer-session.json"
                or name.startswith("runtime/operations/")
                for name in changed
            ), changed)
        finally:
            runtime.shutdown()
            retry_repo.close()

    def test_cancel_queued_operation_is_distinct_and_does_not_validate(self) -> None:
        blocker = BlockingAdapter(result(1))
        self.runtime.shutdown()
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: blocker, operation_workers=1,
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]
        self.repo.artifact(1)
        self.repo.ready(1)
        self.call("cw_request_review", operation_id="blocking-review")
        self.assertTrue(blocker.started.wait(5))
        queued = self.call("cw_validate", operation_id="queued-validation")
        self.assertEqual("QUEUED", queued["status"])
        cancelled = self.call(
            "cw_operation_cancel", operation_id="cancel-request",
            target_operation_id="queued-validation",
        )
        self.assertEqual("CANCELLED", cancelled["status"])
        self.assertEqual("OPERATION_CANCELLED", cancelled["data"]["error"]["code"])
        self.assertFalse(any((self.repo.root / ".cw/validation").glob("*.json")))
        blocker.release.set()
        self.assertEqual("SUCCEEDED", self.wait("blocking-review")["status"])

    def test_running_operation_refuses_unsafe_cancellation(self) -> None:
        blocker = BlockingAdapter(result(1))
        self.runtime.shutdown()
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: blocker, operation_workers=1,
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]
        self.repo.artifact(1)
        self.repo.ready(1)
        self.call("cw_request_review", operation_id="running-review")
        self.assertTrue(blocker.started.wait(5))
        refused = self.call(
            "cw_operation_cancel", operation_id="cancel-running",
            target_operation_id="running-review",
        )
        self.assertEqual("OPERATION_IN_PROGRESS", refused["error"]["code"])
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
        blocker.release.set()
        self.assertEqual("SUCCEEDED", self.wait("running-review")["status"])

    def test_runtime_shutdown_waits_for_supervisor_mutation_boundary(self) -> None:
        blocker = BlockingAdapter(result(1))
        self.runtime.shutdown()
        self.runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: blocker, operation_workers=1,
        )
        self.handle = self.runtime.project_handles()[0]["repository_id"]
        self.repo.artifact(1)
        self.repo.ready(1)
        self.call("cw_request_review", operation_id="review-at-shutdown")
        self.assertTrue(blocker.started.wait(5))
        shutdown = threading.Thread(target=self.runtime.shutdown)
        shutdown.start()
        shutdown.join(timeout=0.05)
        self.assertTrue(shutdown.is_alive())
        self.assertFalse((self.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
        blocker.release.set()
        shutdown.join(timeout=5)
        self.assertFalse(shutdown.is_alive())
        self.assertTrue((self.repo.root / ".cw/gates/01-phase-1.approved.json").is_file())
        self.assertEqual("IN_PROGRESS", self.repo.state()["status"])
        self.assertEqual("02-phase-2", self.repo.state()["current_phase"])

    def test_retry_implementation_is_narrow_and_idempotent(self) -> None:
        state = self.repo.state()
        error = CwError("implementer stopped", ErrorCode.IMPLEMENTER_PROCESS_ERROR)
        state["last_error"] = f"{error.code.value}: {error.message}"
        mark_infrastructure_error(state, error, operation="implementation", phase="01-phase-1")
        transition(self.repo.root, state, WorkflowState.ERROR, force_error=True)
        self.call("cw_retry", operation_id="retry-implementation")
        response = self.wait("retry-implementation")
        self.assertEqual("SUCCEEDED", response["status"])
        self.assertEqual("implementation", response["data"]["result"]["retried"])
        replay = self.call("cw_retry", operation_id="retry-implementation")
        self.assertTrue(replay["idempotent_replay"])
        self.assertTrue((self.repo.root / ".cw/runtime/implementer-session.json").is_file())

    def test_retry_review_preserves_history_and_gate_creation_rules(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        state = self.repo.state()
        error = CwError("reviewer disconnected", ErrorCode.REVIEWER_NETWORK_ERROR)
        state["last_error"] = f"{error.code.value}: {error.message}"
        mark_infrastructure_error(state, error, operation="review", phase="01-phase-1")
        transition(self.repo.root, state, WorkflowState.ERROR, force_error=True)
        self.call("cw_retry", operation_id="retry-review")
        response = self.wait("retry-review")
        self.assertEqual("SUCCEEDED", response["status"])
        self.assertEqual("review", response["data"]["result"]["retried"])
        gate = self.repo.root / ".cw/gates/01-phase-1.approved.json"
        self.assertTrue(gate.is_file())
        original = gate.read_bytes()
        self.call("cw_retry", operation_id="retry-after-valid-gate")
        refused = self.wait("retry-after-valid-gate")
        self.assertEqual("FAILED", refused["status"])
        self.assertEqual("RETRY_NOT_ALLOWED", refused["data"]["error"]["code"])
        self.assertEqual(original, gate.read_bytes())

    def test_retry_without_retryable_error_is_rejected(self) -> None:
        self.call("cw_retry", operation_id="retry-not-allowed")
        response = self.wait("retry-not-allowed")
        self.assertEqual("FAILED", response["status"])
        self.assertEqual("RETRY_NOT_ALLOWED", response["data"]["error"]["code"])

    def test_project_scoped_operation_ids_cannot_cross_projects(self) -> None:
        other = TempRepo(name="other-project")
        runtime = MCPRuntime(
            RuntimeConfig.create([self.repo.root, other.root]), diagnostic_sink=lambda _: None,
            review_backend_factory=lambda: FakeAdapter(result(1)),
        )
        try:
            handles = {item["display_name"]: item["repository_id"] for item in runtime.project_handles()}
            first = handles["sample-app"]
            second = handles["other-project"]
            runtime.call_tool("cw_phase_start", {"project_id": first, "operation_id": "project-a-op"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = runtime.call_tool("cw_operation_status", {
                    "project_id": first, "operation_id": "poll-a",
                    "target_operation_id": "project-a-op",
                })
                if status["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
            cross = runtime.call_tool("cw_operation_status", {
                "project_id": second, "operation_id": "poll-cross",
                "target_operation_id": "project-a-op",
            })
            self.assertEqual("PROJECT_SCOPE_VIOLATION", cross["error"]["code"])
        finally:
            runtime.shutdown()
            other.close()

    def test_concurrent_review_requests_produce_one_gate(self) -> None:
        self.repo.artifact(1)
        self.repo.ready(1)
        responses: list[dict] = []

        def invoke(identifier: str) -> None:
            responses.append(self.call("cw_request_review", operation_id=identifier))

        threads = [threading.Thread(target=invoke, args=(f"review-concurrent-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        finished = [self.wait(f"review-concurrent-{index}") for index in range(2)]
        self.assertEqual(1, sum(item["status"] == "SUCCEEDED" for item in finished))
        self.assertEqual(1, len(list((self.repo.root / ".cw/gates").glob("01-phase-1.approved.json"))))

    def test_cli_project_lock_and_mcp_action_share_conflict_policy(self) -> None:
        with operation_lock(self.repo.root, "cli-review-fixture"):
            self.call("cw_phase_start", operation_id="mcp-during-cli")
            response = self.wait("mcp-during-cli")
        self.assertEqual("BLOCKED", response["status"])
        self.assertEqual("OPERATION_CONFLICT", response["data"]["error"]["code"])
        self.assertFalse((self.repo.root / ".cw/runtime/implementer-session.json").exists())

    def test_operation_files_use_cross_platform_safe_hashed_names(self) -> None:
        identifier = "client:request.with-safe-protocol-id"
        self.call("cw_phase_start", operation_id=identifier)
        self.wait(identifier)
        expected = hashlib.sha256(identifier.encode()).hexdigest() + ".json"
        self.assertTrue((self.repo.root / ".cw/runtime/operations" / expected).is_file())
        self.assertNotIn(":", expected)

    def test_stale_supervisor_operation_reconciles_to_retryable_blocked(self) -> None:
        identifier = "stale-supervisor"
        self.call("cw_phase_start", operation_id=identifier)
        self.assertEqual("SUCCEEDED", self.wait(identifier)["status"])
        name = hashlib.sha256(identifier.encode()).hexdigest() + ".json"
        path = self.repo.root / ".cw/runtime/operations" / name
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update({
            "status": "RUNNING", "stage": "phase_start", "finished_at": None,
            "supervisor_pid": 2_147_483_647, "error": None,
        })
        path.write_text(json.dumps(record), encoding="utf-8")
        response = self.call(
            "cw_operation_status", operation_id="poll-stale",
            target_operation_id=identifier,
        )
        self.assertEqual("BLOCKED", response["status"])
        self.assertEqual("INFRASTRUCTURE_FAILURE", response["data"]["error"]["code"])
        self.assertTrue(response["data"]["error"]["retryable"])

    def test_completion_extension_boundary_cannot_be_crossed_by_mcp(self) -> None:
        from tests.test_mcp_runtime import CompletionFixture, adopt_contract, approve

        repo = TempRepo(name="extension-boundary", phases=1)
        runtime = MCPRuntime(RuntimeConfig.create([repo.root]), diagnostic_sink=lambda _: None)
        try:
            adopt_contract(repo)
            approve(repo)
            gate_bytes = {
                path.name: path.read_bytes() for path in (repo.root / ".cw/gates").glob("*.json")
            }
            fixture = CompletionFixture(repo, "EXTENSION_REQUIRED", missing=True)
            run_completion_review(repo.root, repo.workflow, repo.state(), fixture)
            handle = runtime.project_handles()[0]["repository_id"]
            runtime.call_tool("cw_phase_start", {
                "project_id": handle, "operation_id": "extension-bypass",
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = runtime.call_tool("cw_operation_status", {
                    "project_id": handle, "operation_id": "extension-poll",
                    "target_operation_id": "extension-bypass",
                })
                if response["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
            self.assertEqual("FAILED", response["status"])
            self.assertEqual("COMPLETION_EXTENSION_PENDING", response["data"]["error"]["code"])
            forbidden = runtime.call_tool("cw_authorize_extension", {
                "project_id": handle, "operation_id": "self-approve",
            })
            self.assertEqual("AUTHORIZATION_REQUIRED", forbidden["error"]["code"])
            self.assertEqual(
                gate_bytes,
                {path.name: path.read_bytes() for path in (repo.root / ".cw/gates").glob("*.json")},
            )
        finally:
            runtime.shutdown()
            repo.close()


if __name__ == "__main__":
    unittest.main()
