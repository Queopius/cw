from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cw.cli.main import main
from cw.cli.parser import parse_args
from cw.core.gates import create_gate, gate_path
from cw.core.errors import CwError
from cw.core.errors import ErrorCode
from cw.core.models import WorkflowState
from cw.core.state import advance_after_approval, load_state, save_state, transition
from cw.execution.batch import BatchRunner
from cw.execution.budget import ExecutionBudget
from cw.execution.duration import parse_duration
from cw.execution.estimator import ExecutionEstimator
from cw.execution.config import load_execution_settings, set_execution_setting
from cw.execution.session import load_batch, new_batch, save_batch
from cw.update.config import set_update_setting
from cw.core.toml import load_toml
from tests.helpers import FakeAdapter, TempRepo, result


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class BatchExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=5)
        self.previous = Path.cwd()
        os.chdir(self.repo.root)

    def tearDown(self) -> None:
        os.chdir(self.previous)
        self.repo.close()

    def invoke(self, *args: str) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args)
        return code, stream.getvalue()

    def approve(self, phase_id: str) -> None:
        phase_number = int(phase_id.split("-", 1)[0])
        phase = self.repo.workflow.phase(phase_id)
        self.repo.artifact(phase_number)
        review = self.repo.approved_review(phase_number)
        create_gate(self.repo.root, self.repo.workflow, phase, review)
        state = load_state(self.repo.root)
        advance_after_approval(
            self.repo.root,
            state,
            self.repo.workflow,
            phase,
            gate_path(self.repo.root, phase_id).relative_to(self.repo.root).as_posix(),
            attempt=1,
        )

    def test_duration_parser_accepts_bounded_syntax(self) -> None:
        self.assertEqual(1800, parse_duration("30m"))
        self.assertEqual(5400, parse_duration("90m"))
        self.assertEqual(7200, parse_duration("2h"))
        self.assertEqual(5400, parse_duration("1h30m"))
        for invalid in ("0m", "-1h", "forever", "2days999", "1h90m"):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                parse_duration(invalid)

    def test_runner_completes_exact_phase_budget_and_verifies_gates(self) -> None:
        outcome = BatchRunner().run(
            self.repo.root,
            self.repo.workflow,
            ExecutionBudget(3, 7200, 3),
            lambda phase, _remaining: (self.approve(phase) or 0),
        )
        self.assertEqual("COMPLETED", outcome.status)
        self.assertEqual(3, outcome.completed)
        self.assertEqual("04-phase-4", load_state(self.repo.root)["current_phase"])
        self.assertTrue(all(gate_path(self.repo.root, f"{n:02d}-phase-{n}").is_file() for n in range(1, 4)))
        self.assertFalse(gate_path(self.repo.root, "04-phase-4").exists())

    def test_completed_workflow_runner_never_invokes_executor_or_creates_batch(self) -> None:
        for phase in self.repo.workflow.phases:
            self.approve(phase.id)
        executor = Mock()

        outcome = BatchRunner().run(
            self.repo.root,
            self.repo.workflow,
            ExecutionBudget(3, 7200, 3),
            executor,
        )

        self.assertEqual("COMPLETED", outcome.status)
        self.assertEqual("workflow_complete", outcome.reason)
        self.assertEqual(0, outcome.completed)
        self.assertIsNone(outcome.current_phase)
        executor.assert_not_called()
        self.assertFalse((self.repo.root / ".cw/runtime/batch.json").exists())

    def test_revision_stays_in_phase_and_does_not_consume_phase_budget(self) -> None:
        revised = False

        def execute(phase_id: str, _remaining: float) -> int:
            nonlocal revised
            if phase_id == "02-phase-2" and not revised:
                state = load_state(self.repo.root)
                transition(self.repo.root, state, WorkflowState.READY_FOR_REVIEW)
                transition(self.repo.root, state, WorkflowState.REVIEWING)
                state = load_state(self.repo.root)
                state.setdefault("history", []).append({"phase": phase_id, "action": "revision_required"})
                transition(self.repo.root, state, WorkflowState.REVISION_REQUIRED)
                revised = True
                return 1
            if load_state(self.repo.root)["status"] == WorkflowState.REVISION_REQUIRED.value:
                state = load_state(self.repo.root)
                transition(self.repo.root, state, WorkflowState.IN_PROGRESS)
            self.approve(phase_id)
            return 0

        outcome = BatchRunner().run(
            self.repo.root, self.repo.workflow, ExecutionBudget(3, 7200, 3), execute,
        )
        self.assertEqual(3, outcome.completed)
        session = load_batch(self.repo.root)
        self.assertEqual(1, session["semantic_revisions"])
        self.assertEqual(4, session["agent_runs"])

    def test_human_gate_stops_before_next_phase(self) -> None:
        calls: list[str] = []

        def execute(phase_id: str, _remaining: float) -> int:
            calls.append(phase_id)
            if phase_id == "02-phase-2":
                state = load_state(self.repo.root)
                transition(self.repo.root, state, WorkflowState.READY_FOR_REVIEW)
                transition(self.repo.root, state, WorkflowState.REVIEWING)
                transition(self.repo.root, state, WorkflowState.HUMAN_REVIEW_REQUIRED)
                return 3
            self.approve(phase_id)
            return 0

        outcome = BatchRunner().run(
            self.repo.root, self.repo.workflow, ExecutionBudget(5, 7200, 3), execute,
        )
        self.assertEqual("HUMAN_REVIEW_REQUIRED", outcome.status)
        self.assertEqual(1, outcome.completed)
        self.assertEqual(["01-phase-1", "02-phase-2"], calls)
        self.assertFalse(gate_path(self.repo.root, "03-phase-3").exists())

    def test_time_budget_never_starts_an_extra_phase(self) -> None:
        clock = FakeClock()
        calls: list[str] = []

        def execute(phase_id: str, _remaining: float) -> int:
            calls.append(phase_id)
            clock.advance(30 * 60)
            self.approve(phase_id)
            return 0

        outcome = BatchRunner(clock).run(
            self.repo.root, self.repo.workflow, ExecutionBudget(5, 45 * 60, 3), execute,
        )
        self.assertEqual("BUDGET_EXHAUSTED", outcome.status)
        self.assertEqual(2, outcome.completed)
        self.assertEqual(["01-phase-1", "02-phase-2"], calls)

    def test_dry_run_and_hard_cap_do_not_launch_codex(self) -> None:
        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer:
            code, output = self.invoke("run", "3", "--dry-run", "--no-color")
            blocked, error = self.invoke("run", "50", "--dry-run", "--no-color")
        self.assertEqual(0, code)
        self.assertIn("Batch Preview", output)
        self.assertIn("No work was started", output)
        self.assertEqual(2, blocked)
        self.assertIn("Maximum      10 phases", error)
        implementer.assert_not_called()

    def test_dry_run_json_is_unstyled_and_machine_readable(self) -> None:
        code, output = self.invoke("run", "--phases", "2", "--dry-run", "--json")
        self.assertEqual(0, code)
        payload = json.loads(output)
        self.assertEqual(2, payload["requested_phases"])
        self.assertEqual(["01-phase-1", "02-phase-2"], [item["id"] for item in payload["phases"]])
        self.assertNotIn("\x1b", output)

    def test_cli_run_three_reuses_standard_implement_and_review_path(self) -> None:
        def implement(_root, _prompt, **_kwargs):
            state = load_state(self.repo.root)
            number = int(state["current_phase"].split("-", 1)[0])
            self.repo.artifact(number)
            self.repo.ready(number)
            return 0

        def review(root, workflow, phase, state):
            from cw.agents.reviewer import run_review

            number = int(phase.id.split("-", 1)[0])
            return run_review(root, workflow, phase, state, FakeAdapter(result(number)))

        with (
            patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=implement) as implementer,
            patch("cw.cli.main.run_review", side_effect=review) as reviewer,
        ):
            code, output = self.invoke("run", "3", "--no-color")
        self.assertEqual(0, code)
        self.assertIn("Batch Complete", output)
        self.assertEqual(3, implementer.call_count)
        self.assertEqual(3, reviewer.call_count)
        self.assertEqual("04-phase-4", load_state(self.repo.root)["current_phase"])

    def test_until_and_conflicting_flags(self) -> None:
        code, output = self.invoke("run", "--until", "03-phase-3", "--dry-run", "--no-color")
        self.assertEqual(0, code)
        self.assertIn("3 phases", output)
        code, output = self.invoke("run", "2", "--until", "03-phase-3", "--dry-run")
        self.assertEqual(2, code)
        self.assertIn("cannot be combined", output)

    def test_default_zero_negative_non_number_and_unknown_until(self) -> None:
        code, output = self.invoke("run", "--dry-run", "--json")
        self.assertEqual(0, code)
        self.assertEqual(1, json.loads(output)["requested_phases"])
        for value in ("0", "-1"):
            with self.subTest(value=value):
                code, _ = self.invoke("run", value, "--dry-run")
                self.assertEqual(2, code)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["run", "many"])
        code, output = self.invoke("run", "--until", "99-missing", "--dry-run")
        self.assertEqual(2, code)
        self.assertIn("Unknown target phase", output)

    def test_completed_batch_cannot_be_resumed(self) -> None:
        BatchRunner().run(
            self.repo.root, self.repo.workflow, ExecutionBudget(1, 7200, 3),
            lambda phase, _remaining: (self.approve(phase) or 0),
        )
        code, output = self.invoke("run", "--resume", "--no-color")
        self.assertEqual(3, code)
        self.assertIn("No safely resumable batch", output)

    def test_resume_preserves_original_phase_and_time_budget(self) -> None:
        self.approve("01-phase-1")
        session = new_batch("01-phase-1", 3, 7200, __import__("cw").__version__)
        session.update({"status": "STOPPED", "completed_phases": 1, "elapsed_seconds": 3600, "pid": None})
        save_batch(self.repo.root, session)
        clock = FakeClock()

        def execute(phase_id: str, _remaining: float) -> int:
            clock.advance(600)
            self.approve(phase_id)
            return 0

        outcome = BatchRunner(clock).run(
            self.repo.root,
            self.repo.workflow,
            ExecutionBudget(3, 7200, 3),
            execute,
            session=session,
        )
        self.assertEqual(3, outcome.completed)
        self.assertEqual(4800, outcome.elapsed_seconds)
        self.assertEqual("04-phase-4", load_state(self.repo.root)["current_phase"])

    def test_stale_running_batch_is_recognized_as_interrupted_for_resume(self) -> None:
        session = new_batch("01-phase-1", 2, 7200, __import__("cw").__version__)
        session["pid"] = 999_999_999
        save_batch(self.repo.root, session)
        with patch("cw.cli.commands.batch.BatchRunner.run") as run:
            run.return_value = type("Outcome", (), {
                "status": "STOPPED", "completed": 0, "requested": 2,
                "elapsed_seconds": 0, "reason": "test", "current_phase": "01-phase-1",
                "exit_code": 4,
            })()
            code, _ = self.invoke("run", "--resume", "--no-color")
        self.assertEqual(4, code)
        self.assertEqual("INTERRUPTED", run.call_args.kwargs["session"]["status"])

    def test_keyboard_interrupt_preserves_current_phase_and_marks_session_stopped(self) -> None:
        outcome = BatchRunner().run(
            self.repo.root,
            self.repo.workflow,
            ExecutionBudget(3, 7200, 3),
            lambda _phase, _remaining: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        self.assertEqual(130, outcome.exit_code)
        self.assertEqual("user_interrupted", outcome.reason)
        self.assertEqual("STOPPED", load_batch(self.repo.root)["status"])
        self.assertEqual("01-phase-1", load_state(self.repo.root)["current_phase"])
        self.assertFalse(gate_path(self.repo.root, "01-phase-1").exists())

    def test_missing_gate_fails_closed_and_finalizes_session(self) -> None:
        def unsafe_advance(_phase: str, _remaining: float) -> int:
            state = load_state(self.repo.root)
            state["current_phase"] = "02-phase-2"
            save_state(self.repo.root, state)
            return 0

        with self.assertRaises(CwError):
            BatchRunner().run(
                self.repo.root, self.repo.workflow, ExecutionBudget(2, 7200, 3), unsafe_advance,
            )
        self.assertEqual("FAILED", load_batch(self.repo.root)["status"])
        self.assertFalse(gate_path(self.repo.root, "01-phase-1").exists())

    def test_batch_completion_revalidates_every_gate_created_in_session(self) -> None:
        def execute(phase_id: str, _remaining: float) -> int:
            self.approve(phase_id)
            if phase_id == "02-phase-2":
                self.repo.artifact(1, "changed after approval\n")
            return 0

        with self.assertRaises(CwError):
            BatchRunner().run(
                self.repo.root, self.repo.workflow, ExecutionBudget(2, 7200, 3), execute,
            )
        self.assertEqual("FAILED", load_batch(self.repo.root)["status"])

    def test_revision_budget_exhaustion_never_starts_next_phase(self) -> None:
        calls = 0

        def revise(phase_id: str, _remaining: float) -> int:
            nonlocal calls
            calls += 1
            state = load_state(self.repo.root)
            if state["status"] == WorkflowState.REVISION_REQUIRED.value:
                transition(self.repo.root, state, WorkflowState.IN_PROGRESS)
                state = load_state(self.repo.root)
            transition(self.repo.root, state, WorkflowState.READY_FOR_REVIEW)
            transition(self.repo.root, state, WorkflowState.REVIEWING)
            state = load_state(self.repo.root)
            state.setdefault("history", []).append({"phase": phase_id, "action": "revision_required"})
            transition(self.repo.root, state, WorkflowState.REVISION_REQUIRED)
            return 1

        outcome = BatchRunner().run(
            self.repo.root, self.repo.workflow, ExecutionBudget(3, 7200, 2), revise,
        )
        self.assertEqual("FAILED", outcome.status)
        self.assertEqual("semantic_revision_budget_reached", outcome.reason)
        self.assertEqual(2, calls)
        self.assertEqual("01-phase-1", load_state(self.repo.root)["current_phase"])

    def test_required_integration_failure_stops_before_implementer(self) -> None:
        failure = CwError("Required integration unavailable", ErrorCode.MCP_REQUIRED_UNAVAILABLE)
        with (
            patch("cw.cli.commands.execution.IntegrationManager.preflight", side_effect=failure),
            patch("cw.cli.main.CodexAdapter.run_implementer") as implementer,
        ):
            code, output = self.invoke("run", "2", "--no-color")
        self.assertEqual(1, code)
        self.assertIn("Required integration unavailable", output)
        implementer.assert_not_called()
        self.assertEqual("FAILED", load_batch(self.repo.root)["status"])
        self.assertFalse(gate_path(self.repo.root, "01-phase-1").exists())

    def test_infrastructure_failure_consumes_no_phase_or_semantic_revision(self) -> None:
        failure = CwError("offline", ErrorCode.IMPLEMENTER_PROCESS_ERROR)
        with self.assertRaises(CwError):
            BatchRunner().run(
                self.repo.root,
                self.repo.workflow,
                ExecutionBudget(3, 7200, 3),
                lambda _phase, _remaining: (_ for _ in ()).throw(failure),
            )
        session = load_batch(self.repo.root)
        self.assertEqual(0, session["completed_phases"])
        self.assertEqual(0, session["semantic_revisions"])
        self.assertEqual(0, load_state(self.repo.root)["attempt"])

    def test_yes_never_bypasses_default_hard_cap(self) -> None:
        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer:
            code, output = self.invoke("run", "50", "--yes", "--no-color")
        self.assertEqual(2, code)
        self.assertIn("Batch too large", output)
        implementer.assert_not_called()

    def test_estimator_uses_structured_durations_or_reports_unavailable(self) -> None:
        estimator = ExecutionEstimator()
        self.assertEqual("unavailable", estimator.estimate([], 2).basis)
        estimate = estimator.estimate([], 2, completed_durations=[600, 1200, 900])
        self.assertEqual("project-history", estimate.basis)
        self.assertEqual("medium", estimate.confidence)
        self.assertLess(estimate.minimum_seconds, estimate.maximum_seconds)

    def test_global_execution_settings_are_validated_and_preserved(self) -> None:
        with TemporaryDirectory() as config_home, patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home}):
            set_execution_setting("execution.hard_max_phases", "8")
            set_execution_setting("execution.default_max_time", "90m")
            set_update_setting("updates.channel", "beta")
            settings = load_execution_settings(self.repo.root)
            document = load_toml(Path(config_home) / "cw/config.toml")
        self.assertEqual(8, settings.hard_max_phases)
        self.assertEqual(5400, settings.default_max_time_seconds)
        self.assertEqual("90m", document["execution"]["default_max_time"])
        self.assertEqual("beta", document["updates"]["channel"])

    def test_project_execution_policy_can_only_reduce_global_caps(self) -> None:
        config = self.repo.root / ".cw/config.toml"
        config.write_text("[execution]\nmax_phases = 4\nmax_time = \"90m\"\n", encoding="utf-8")
        settings = load_execution_settings(self.repo.root)
        self.assertEqual(4, settings.hard_max_phases)
        self.assertEqual(5400, settings.default_max_time_seconds)


if __name__ == "__main__":
    unittest.main()
