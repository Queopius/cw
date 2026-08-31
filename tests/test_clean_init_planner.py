from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.adapters.codex import CodexAdapter, CodexResult
from cw.adapters.structured_output import codex_schema, validate_codex_output_schema
from cw.cli.main import main
from cw.core.errors import CwError, ErrorCode
from cw.core.models import WorkflowState
from cw.core.state import load_state, save_state, transition
from cw.core.workflow import load_workflow
from cw.planning.planner import Planner


class CleanRepository:
    def __init__(self, name: str = "clean-project", readme: str = "# Demo project\n") -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-clean-")
        self.root = Path(self.temporary.name) / name
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "config", "user.email", "tests@example.test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "CW Tests"], cwd=self.root, check=True)
        (self.root / "README.md").write_text(readme, encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "Initial"], cwd=self.root, check=True)

    def close(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *args: str) -> tuple[int, str]:
        previous = Path.cwd()
        stream = io.StringIO()
        try:
            os.chdir(self.root)
            with redirect_stdout(stream):
                code = main(args)
        finally:
            os.chdir(previous)
        return code, stream.getvalue()


class CleanInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = CleanRepository()

    def tearDown(self) -> None:
        self.repo.close()

    def test_clean_git_repository_initializes_as_initialized(self) -> None:
        code, output = self.repo.invoke("init")
        self.assertEqual(0, code)
        self.assertIn("Workflow initialized", output)
        self.assertEqual("INITIALIZED", load_state(self.repo.root)["status"])
        self.assertEqual("NOT_CREATED", load_workflow(self.repo.root).status)

    def test_idempotent_init_migrates_legacy_uninitialized_state(self) -> None:
        self.repo.invoke("init")
        state_path = self.repo.root / ".cw/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "UNINITIALIZED"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        code, _ = self.repo.invoke("init")
        self.assertEqual(0, code)
        self.assertEqual("INITIALIZED", load_state(self.repo.root)["status"])

    def test_no_plan_status_is_dedicated_and_actionable(self) -> None:
        self.repo.invoke("init")
        code, output = self.repo.invoke("status", "--no-color")
        self.assertEqual(0, code)
        self.assertIn("○ INITIALIZED", output)
        self.assertIn("No development plan exists yet.", output)
        self.assertIn("cw plan", output)
        for forbidden in ("Progress", "0 / 0", "Gate", "Readiness", "cw validate", "Continue development"):
            self.assertNotIn(forbidden, output)

    def test_no_plan_json_reports_initialized_without_phase(self) -> None:
        self.repo.invoke("init")
        code, output = self.repo.invoke("status", "--json")
        payload = json.loads(output)
        self.assertEqual(0, code)
        self.assertEqual("INITIALIZED", payload["state"])
        self.assertEqual("INITIALIZED", payload["workflow"])
        self.assertIsNone(payload["phase"])
        self.assertEqual(0, payload["phase_count"])

    def test_start_without_plan_never_invokes_implementer(self) -> None:
        self.repo.invoke("init")
        with patch("cw.cli.main.CodexAdapter.run_implementer") as implementer:
            code, output = self.repo.invoke("start")
        self.assertEqual(3, code)
        self.assertIn("Development plan required", output)
        self.assertIn("cw plan", output)
        implementer.assert_not_called()

    def test_validate_without_plan_never_runs_validation(self) -> None:
        self.repo.invoke("init")
        with patch("cw.cli.commands.execution.validate_phase") as validation:
            code, output = self.repo.invoke("validate")
        self.assertEqual(3, code)
        self.assertIn("Nothing to validate", output)
        self.assertIn("cw plan", output)
        validation.assert_not_called()

    def test_minimal_readme_is_not_hallucinated_into_a_goal(self) -> None:
        self.repo.invoke("init")
        with patch("cw.cli.main.CodexAdapter.run_planner") as planner:
            code, output = self.repo.invoke("plan")
        self.assertEqual(3, code)
        self.assertIn("Project goal is unclear", output)
        self.assertIn('cw plan --goal "Describe what you want to build"', output)
        self.assertEqual("INITIALIZED", load_state(self.repo.root)["status"])
        planner.assert_not_called()

    def test_explicit_goal_produces_proposed_project_plan(self) -> None:
        self.repo.invoke("init")
        goal = "Build a small Laravel REST API for inventory management"
        proposal = Planner().propose_plan(self.repo.root, "clean-project", goal)
        with patch(
            "cw.cli.main.CodexAdapter.run_planner",
            return_value=CodexResult({"phases": proposal["phases"]}, "MCP startup warning"),
        ):
            code, output = self.repo.invoke("plan", "--goal", goal)
        self.assertEqual(0, code)
        self.assertIn("Plan proposed", output)
        self.assertNotIn("MCP startup warning", output)
        workflow = load_workflow(self.repo.root)
        self.assertEqual("PROPOSED", workflow.status)
        self.assertEqual(goal, workflow.goal)

    def test_verbose_plan_exposes_captured_stderr_without_affecting_success(self) -> None:
        self.repo.invoke("init")
        goal = "Build an inventory API"
        proposal = Planner().propose_plan(self.repo.root, "clean-project", goal)
        with patch(
            "cw.cli.main.CodexAdapter.run_planner",
            return_value=CodexResult({"phases": proposal["phases"]}, "MCP startup warning", "event stream"),
        ):
            code, output = self.repo.invoke("plan", "--goal", goal, "--verbose")
        self.assertEqual(0, code)
        self.assertIn("Planner diagnostics", output)
        self.assertIn("MCP startup warning", output)


class StructuredOutputCompatibilityTests(unittest.TestCase):
    def test_codex_facing_plan_and_review_schemas_have_no_unique_items(self) -> None:
        for name in ("plan-output.schema.json", "review-output.schema.json"):
            path = codex_schema(name)
            self.assertNotIn("uniqueItems", path.read_text(encoding="utf-8"))
            validate_codex_output_schema(path, role="planner" if name.startswith("plan") else "reviewer")

    def test_internal_schema_retains_stronger_uniqueness_contract(self) -> None:
        internal = Path(__file__).parents[1] / "cw/schemas/plan-proposal.schema.json"
        self.assertIn("uniqueItems", internal.read_text(encoding="utf-8"))

    def _duplicate_plan(self, key: str) -> tuple[Path, dict]:
        temporary = tempfile.TemporaryDirectory(prefix="cw-duplicate-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        payload = Planner().propose_plan(root, "duplicate-project", "Build an inventory API")
        if key == "depends_on":
            payload["phases"][1][key] = [payload["phases"][0]["id"]] * 2
        elif key == "acceptance_criteria":
            payload["phases"][0][key].append(dict(payload["phases"][0][key][0]))
        else:
            payload["phases"][0][key].append(payload["phases"][0][key][0])
        return root, payload

    def test_internal_validation_rejects_duplicate_dependencies(self) -> None:
        root, payload = self._duplicate_plan("depends_on")
        with self.assertRaises(CwError):
            Planner().validate_plan(root, payload)

    def test_internal_validation_rejects_duplicate_artifacts(self) -> None:
        root, payload = self._duplicate_plan("artifacts")
        with self.assertRaises(CwError):
            Planner().validate_plan(root, payload)

    def test_internal_validation_rejects_duplicate_review_paths(self) -> None:
        root, payload = self._duplicate_plan("review_paths")
        with self.assertRaises(CwError):
            Planner().validate_plan(root, payload)

    def test_internal_validation_rejects_duplicate_criterion_ids(self) -> None:
        root, payload = self._duplicate_plan("acceptance_criteria")
        with self.assertRaises(CwError):
            Planner().validate_plan(root, payload)

    def test_success_ignores_mcp_stderr_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = codex_schema("plan-output.schema.json")

            def fake_run(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text('{"phases": []}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "valid stdout", "ERROR MCP HTTP 500")

            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=fake_run,
            ):
                result = CodexAdapter().run_planner(root, "plan", schema, 10)
        self.assertEqual([], result.payload["phases"])
        self.assertIn("MCP HTTP 500", result.stderr)
        self.assertEqual("valid stdout", result.stdout)

    def test_terminal_invalid_schema_outranks_mcp_transport_noise(self) -> None:
        diagnostic = "MCP transport HTTP 500\ninvalid_request_error invalid_json_schema uniqueItems is not permitted"
        code = CodexAdapter.classify_process_error(diagnostic, role="planner")
        self.assertEqual(ErrorCode.PLANNER_SCHEMA_ERROR, code)
        self.assertNotEqual(ErrorCode.PLANNER_NETWORK_ERROR, code)

    def test_http_400_invalid_schema_from_process_is_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failure = subprocess.CompletedProcess(
                [], 1, "",
                'MCP HTTP 500\n{"code":"invalid_json_schema","status":400}',
            )
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", return_value=failure,
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_planner(root, "plan", codex_schema("plan-output.schema.json"), 10)
        self.assertEqual(ErrorCode.PLANNER_SCHEMA_ERROR, raised.exception.code)
        self.assertEqual("Run: cw error", raised.exception.hint)
        self.assertIn("STDERR", raised.exception.details or "")

    def test_incompatible_codex_schema_is_rejected_before_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "bad.schema.json"
            schema.write_text('{"type":"array","uniqueItems":true}', encoding="utf-8")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run",
            ) as process, self.assertRaises(CwError) as raised:
                CodexAdapter().run_planner(root, "plan", schema, 10)
        self.assertEqual(ErrorCode.PLANNER_SCHEMA_ERROR, raised.exception.code)
        process.assert_not_called()

    def test_nonzero_unknown_exit_is_process_failure(self) -> None:
        self.assertEqual(
            ErrorCode.PLANNER_PROCESS_ERROR,
            CodexAdapter.classify_process_error("planner process crashed", role="planner"),
        )

    def test_schema_error_is_not_retryable(self) -> None:
        repo = CleanRepository()
        try:
            repo.invoke("init")
            failure = CwError(
                "Codex planner unavailable", ErrorCode.PLANNER_SCHEMA_ERROR,
                "Run: cw error", details="invalid_json_schema",
            )
            with patch("cw.cli.main.CodexAdapter.run_planner", side_effect=failure):
                code, output = repo.invoke("plan", "--goal", "Build inventory API")
            self.assertEqual(1, code)
            self.assertIn("Planner schema incompatible", output)
            self.assertNotIn("cw retry", output)
            state = load_state(repo.root)
            self.assertEqual("INITIALIZED", state["status"])
            self.assertIsNone(state["infrastructure_error"])
        finally:
            repo.close()

    def test_transport_failure_is_retryable_planning_context(self) -> None:
        repo = CleanRepository()
        try:
            repo.invoke("init")
            failure = CwError(
                "Planner transport failed", ErrorCode.PLANNER_TRANSPORT_ERROR,
                "Run: cw retry", details="websocket closed",
            )
            with patch("cw.cli.main.CodexAdapter.run_planner", side_effect=failure):
                code, output = repo.invoke("plan", "--goal", "Build inventory API")
            self.assertEqual(1, code)
            self.assertIn("cw retry", output)
            state = load_state(repo.root)
            self.assertEqual("ERROR", state["status"])
            self.assertEqual("planning", state["infrastructure_error"]["operation"])
            self.assertTrue(state["infrastructure_error"]["retryable"])
        finally:
            repo.close()

    def test_interrupted_planning_records_retryable_failure_and_retry_succeeds(self) -> None:
        repo = CleanRepository()
        try:
            repo.invoke("init")
            goal = "Build inventory API"
            with patch("cw.cli.main.CodexAdapter.run_planner", side_effect=KeyboardInterrupt):
                code, _ = repo.invoke("plan", "--goal", goal)
            self.assertEqual(130, code)
            failed = load_state(repo.root)
            self.assertEqual("ERROR", failed["status"])
            self.assertEqual("PLANNER_TRANSPORT_ERROR", failed["infrastructure_error"]["error_code"])
            self.assertEqual(goal, failed["pending_goal"])
            proposal = Planner().propose_plan(repo.root, "clean-project", goal)
            with patch(
                "cw.cli.main.CodexAdapter.run_planner",
                return_value=CodexResult({"phases": proposal["phases"]}, ""),
            ):
                retry_code, _ = repo.invoke("retry")
            self.assertEqual(0, retry_code)
            self.assertEqual("PLAN_PROPOSED", load_state(repo.root)["status"])
        finally:
            repo.close()

    def test_restart_recovers_stranded_planning_without_manual_state_edit(self) -> None:
        repo = CleanRepository()
        try:
            repo.invoke("init")
            goal = "Build inventory API"
            state = load_state(repo.root)
            state["pending_goal"] = goal
            save_state(repo.root, state)
            transition(repo.root, state, WorkflowState.PLANNING)
            # A fresh CLI invocation models restart after the process died
            # between the durable PLANNING transition and child launch.
            proposal = Planner().propose_plan(repo.root, "clean-project", goal)
            with patch(
                "cw.cli.main.CodexAdapter.run_planner",
                return_value=CodexResult({"phases": proposal["phases"]}, ""),
            ):
                code, output = repo.invoke("retry")
            self.assertEqual(0, code, output)
            recovered = load_state(repo.root)
            self.assertEqual("PLAN_PROPOSED", recovered["status"])
            actions = [event["action"] for event in recovered["history"]]
            self.assertEqual(1, actions.count("planning_failure_recovered"))
            self.assertEqual(1, actions.count("retry_started"))
            self.assertEqual("PROPOSED", load_workflow(repo.root).status)
            retry_again, _ = repo.invoke("retry")
            self.assertEqual(1, retry_again)
            self.assertEqual("PLAN_PROPOSED", load_state(repo.root)["status"])
        finally:
            repo.close()

    def test_partial_planning_state_is_not_recovered_as_an_empty_plan(self) -> None:
        repo = CleanRepository()
        try:
            repo.invoke("init")
            state = load_state(repo.root)
            state["pending_goal"] = "Build inventory API"
            state["workflow_sha256"] = "sha256:" + "a" * 64
            save_state(repo.root, state)
            transition(repo.root, state, WorkflowState.PLANNING)
            code, output = repo.invoke("retry")
            self.assertEqual(1, code)
            self.assertIn("not safe to retry", output)
            self.assertEqual("PLANNING", load_state(repo.root)["status"])
            self.assertEqual("NOT_CREATED", load_workflow(repo.root).status)
        finally:
            repo.close()

    def test_malformed_planner_output_returns_to_initialized_and_can_be_planned_again(self) -> None:
        repo = CleanRepository()
        try:
            repo.invoke("init")
            goal = "Build inventory API"
            malformed = CodexResult({"unexpected": []}, "")
            with patch("cw.cli.main.CodexAdapter.run_planner", return_value=malformed):
                code, _ = repo.invoke("plan", "--goal", goal)
            self.assertEqual(1, code)
            self.assertEqual("INITIALIZED", load_state(repo.root)["status"])
            self.assertEqual("NOT_CREATED", load_workflow(repo.root).status)
            proposal = Planner().propose_plan(repo.root, "clean-project", goal)
            with patch(
                "cw.cli.main.CodexAdapter.run_planner",
                return_value=CodexResult({"phases": proposal["phases"]}, ""),
            ):
                retry_code, _ = repo.invoke("plan", "--goal", goal)
            self.assertEqual(0, retry_code)
            self.assertEqual("PLAN_PROPOSED", load_state(repo.root)["status"])
        finally:
            repo.close()

    def test_retry_succeeds_after_process_transport_and_timeout_failures(self) -> None:
        failures = (
            ErrorCode.PLANNER_PROCESS_ERROR,
            ErrorCode.PLANNER_TRANSPORT_ERROR,
            ErrorCode.PLAN_TIMEOUT,
        )
        for error_code in failures:
            with self.subTest(error_code=error_code.value):
                repo = CleanRepository()
                try:
                    repo.invoke("init")
                    goal = "Build inventory API"
                    failure = CwError(
                        "Planner failed", error_code, "Run: cw retry",
                        details="provider=codex mode=stdin retry_safe=true",
                    )
                    with patch("cw.cli.main.CodexAdapter.run_planner", side_effect=failure):
                        first_code, _ = repo.invoke("plan", "--goal", goal)
                    self.assertEqual(1, first_code)
                    self.assertEqual("ERROR", load_state(repo.root)["status"])
                    proposal = Planner().propose_plan(repo.root, "clean-project", goal)
                    with patch(
                        "cw.cli.main.CodexAdapter.run_planner",
                        return_value=CodexResult({"phases": proposal["phases"]}, ""),
                    ):
                        retry_code, _ = repo.invoke("retry")
                    self.assertEqual(0, retry_code)
                    self.assertEqual("PLAN_PROPOSED", load_state(repo.root)["status"])
                finally:
                    repo.close()

    def test_planner_is_external_read_only_child_and_not_a_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fake_run(command, **kwargs):
                self.assertIn("exec", command)
                self.assertIn("read-only", command)
                disable_pairs = [command[index:index + 2] for index in range(len(command) - 1)]
                self.assertNotIn(["--disable", "plugins"], disable_pairs)
                self.assertIn(["--disable", "hooks"], disable_pairs)
                self.assertFalse(any("mcp_servers." in value for value in command))
                self.assertNotIn("phase_gate.py", " ".join(command))
                self.assertEqual("1", kwargs["env"]["CW_PLANNER_ACTIVE"])
                self.assertNotIn("CW_IMPLEMENTER_ACTIVE", kwargs["env"])
                self.assertEqual(os.environ.get("CODEX_HOME"), kwargs["env"].get("CODEX_HOME"))
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text('{"phases": []}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=fake_run,
            ):
                CodexAdapter().run_planner(root, "plan", codex_schema("plan-output.schema.json"), 10)


class PlanningIsolationTests(unittest.TestCase):
    def test_two_projects_receive_distinct_plans_without_foreign_terms(self) -> None:
        first = CleanRepository("plan-demo-a", "# Alpha service\n\nAn API for orchard stock and harvest records.\n")
        second = CleanRepository("plan-demo-b", "# Beta tool\n\nA command line tool for astronomy observation notes.\n")
        try:
            first.invoke("init")
            second.invoke("init")
            goals = {
                first.root: "Build an orchard inventory REST API",
                second.root: "Build an astronomy observation CLI",
            }

            def backend(root, prompt, schema, timeout):
                project = root.name
                proposal = Planner().propose_plan(root, project, goals[root])
                return CodexResult({"phases": proposal["phases"]}, "")

            with patch("cw.cli.main.CodexAdapter.run_planner", side_effect=backend):
                self.assertEqual(0, first.invoke("plan", "--goal", goals[first.root])[0])
                self.assertEqual(0, second.invoke("plan", "--goal", goals[second.root])[0])
            plan_a = (first.root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8")
            plan_b = (second.root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8")
            self.assertNotEqual(plan_a, plan_b)
            self.assertEqual("plan-demo-a", load_state(first.root)["workflow_id"])
            self.assertEqual("plan-demo-b", load_state(second.root)["workflow_id"])
            self.assertNotIn("astronomy", plan_a.lower())
            self.assertNotIn("orchard", plan_b.lower())
            for foreign in ("moloni", "amazon", "mapping-engine"):
                self.assertNotIn(foreign, (plan_a + plan_b).lower())
        finally:
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
