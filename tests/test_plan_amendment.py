from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from cw.cli.main import main
from cw.cli.parser import build_parser
from cw.core.completion import contract_hash
from cw.core.errors import CwError, ErrorCode
from cw.core.initialize import backup_metadata, initialize
from cw.core.models import WorkflowState
from cw.core.plan_amendment import TRANSACTION, amend_plan
from cw.core.plan_amendment import (
    apply_active_artifact_amendment,
    audit_evidence_supersessions,
    prepare_active_artifact_amendment,
)
from cw.core.gates import artifact_hashes
from cw.core.locking import operation_lock
from cw.core.state import bind_plan, load_state, save_state, transition, validate_state
from cw.core.utils import atomic_json, sha256_file
from cw.core.workflow import (
    _read_document,
    load_workflow,
    workflow_document_from_text,
    workflow_hash,
    write_workflow,
)
from tests.helpers import FakeAdapter, TempRepo, result
from cw.agents.reviewer import run_review
from cw.application.actions import validate_current_phase
from cw.application.projects import ProjectHandle, ResolvedProject

FIXTURES = Path(__file__).parent / "fixtures" / "plan-amend"


class ProposedRepo:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-plan-amend-")
        self.root = Path(self.temporary.name) / "bridge-fixture"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        initialize(self.root)
        write_workflow(
            self.root / ".codex/workflow/phases.yaml",
            _read_document(FIXTURES / "moloni-incomplete.yaml"),
        )
        workflow = load_workflow(self.root)
        state = load_state(self.root)
        transition(self.root, state, WorkflowState.PLANNING)
        bind_plan(self.root, state, workflow)
        transition(self.root, state, WorkflowState.PLAN_PROPOSED)
        shutil.copy2(
            FIXTURES / "moloni-corrected.yaml", self.root / "corrected-phases.yaml"
        )

    def close(self) -> None:
        self.temporary.cleanup()

    @property
    def sha(self) -> str:
        return workflow_hash(self.root / ".codex/workflow/phases.yaml")

    def amend(self, **kwargs):
        return amend_plan(self.root, "corrected-phases.yaml", self.sha, **kwargs)


class ActiveArtifactRepo:
    """Neutral active-workflow fixture with incompatible current-phase evidence."""

    def __init__(
        self,
        *,
        reviews: int = 3,
        phase_id: str = "01-phase-1",
        addition: str = "tests/Fixtures/Contracts/example.graphql",
        legacy_missing_supersessions: bool = False,
        legacy_missing_plan_revisions: bool = False,
    ) -> None:
        self.repo = TempRepo(name="generic-marketplace-bridge", phases=2)
        self.root = self.repo.root
        self.phase_id = phase_id
        document = _read_document(self.root / ".codex/workflow/phases.yaml")
        document["phases"][0]["id"] = phase_id
        document["phases"][1]["depends_on"] = [phase_id]
        document["phases"][0]["review_paths"].append("tests/Fixtures/Contracts/**/*")
        document["completion_target"] = {
            "id": "controlled-pilot",
            "name": "Controlled pilot",
            "description": "Neutral fixture completion boundary",
            "target_type": "controlled-pilot",
            "requirements": [{
                "id": "CONTRACT_INTEGRITY",
                "description": "The declared completion contract remains intact",
                "severity": "blocking",
                "evidence_expectations": ["canonical contract hash"],
            }],
        }
        write_workflow(self.root / ".codex/workflow/phases.yaml", document)
        state = load_state(self.root)
        state["current_phase"] = phase_id
        state["workflow_sha256"] = workflow_hash(self.root / ".codex/workflow/phases.yaml")
        save_state(self.root, state)
        self.repo.workflow = load_workflow(self.root)
        self.repo.artifact()
        project = ResolvedProject(
            self.root,
            self.repo.project,
            ProjectHandle(self.repo.project.project_id, "generic-fixture", self.root.name),
        )
        validate_current_phase(project, "generic-validation-before-amendment")
        self.addition = addition
        target = self.root / self.addition
        target.parent.mkdir(parents=True)
        target.write_text("type Query { example: String! }\n", encoding="utf-8")
        readiness = {
            "schema_version": 1,
            "phase": phase_id,
            "status": "READY_FOR_REVIEW",
            "artifacts": ["docs/phase-1.md", addition],
            "checks_executed": [],
            "session_id": "0" * 32,
        }
        atomic_json(self.root / ".cw/runtime/READY_FOR_REVIEW.json", readiness)
        old_hashes = artifact_hashes(self.root, self.repo.workflow.phases[0].artifacts)
        for attempt in range(1, reviews + 1):
            review = {
                "schema_version": 1,
                "workflow": self.repo.workflow.id,
                "phase": phase_id,
                "attempt": attempt,
                "kind": "semantic_review",
                "decision": "REVISE",
                "summary": "Artifact declaration is incomplete",
                "criteria": [{
                    "id": "P1-001", "status": "FAIL", "severity": "blocking",
                    "evidence": ["docs/phase-1.md:1 incomplete manifest"],
                }],
                "blocking_criteria": [],
                "blocking_issues": ["Declare the existing contract fixture", "P1-001"],
                "artifact_hashes": old_hashes,
                "created_at": f"2026-08-21T12:00:0{attempt}Z",
            }
            path = self.root / ".cw/reviews" / f"{phase_id}-attempt-{attempt:02d}.json"
            atomic_json(path, review)
            state["last_review"] = path.relative_to(self.root).as_posix()
            state.setdefault("history", []).append({
                "timestamp": review["created_at"], "phase": phase_id,
                "action": "revision_required", "attempt": attempt,
                "issues": review["blocking_issues"],
            })
        state["attempt"] = reviews
        state["revision_attempt"] = reviews
        state["status"] = WorkflowState.REVISION_REQUIRED.value
        if legacy_missing_supersessions or legacy_missing_plan_revisions:
            state["created_with_cw_version"] = "0.14.1"
            state["active_plan_revision"] = None
            state["active_plan_revision_sha256"] = None
            state["superseded_plan_revisions"] = []
        save_state(self.root, state)
        if legacy_missing_supersessions:
            shutil.rmtree(self.root / ".cw/supersessions")
        if legacy_missing_plan_revisions:
            shutil.rmtree(self.root / ".cw/plan-revisions")

    @property
    def workflow_sha(self) -> str:
        return workflow_hash(self.root / ".codex/workflow/phases.yaml")

    @property
    def state_sha(self) -> str:
        return sha256_file(self.root / ".cw/state.json")

    def apply(self, **kwargs):
        return apply_active_artifact_amendment(
            self.root, self.phase_id, [self.addition], self.workflow_sha,
            self.state_sha, "Declare an existing omitted contract artifact", **kwargs,
        )

    def close(self) -> None:
        self.repo.close()


class PlanAmendmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = ProposedRepo()

    def tearDown(self) -> None:
        self.repo.close()

    def snapshots(self) -> tuple[bytes, bytes]:
        return (
            (self.repo.root / ".codex/workflow/phases.yaml").read_bytes(),
            (self.repo.root / ".cw/state.json").read_bytes(),
        )

    def test_parser_and_public_help_expose_exact_command(self) -> None:
        args = build_parser().parse_args(
            [
                "plan",
                "amend",
                "--file",
                "corrected-phases.yaml",
                "--expected-workflow-sha256",
                "0" * 64,
            ]
        )
        self.assertEqual("amend", args.action)
        output = subprocess.run(
            [__import__("sys").executable, "-m", "cw", "plan", "--help"],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertIn("amend", output)
        self.assertIn("--expected-workflow-sha256", output)

    def test_moloni_regression_amends_without_approval_or_execution(self) -> None:
        before_contract = contract_hash(load_workflow(self.repo.root).completion_target)  # type: ignore[arg-type]
        before_workflow = (self.repo.root / ".codex/workflow/phases.yaml").read_bytes()
        before_state_hash = sha256_file(self.repo.root / ".cw/state.json")
        output = self.repo.amend()
        workflow = load_workflow(self.repo.root)
        state = load_state(self.repo.root)
        self.assertTrue(output["amended"])
        self.assertEqual("PLAN_PROPOSED", output["status"])
        self.assertEqual(11, output["phase_count"])
        self.assertTrue(output["completion_contract_preserved"])
        self.assertTrue(output["approval_required"])
        self.assertEqual("PROPOSED", workflow.status)
        self.assertEqual("PLAN_PROPOSED", state["status"])
        self.assertEqual("01-contracts", state["current_phase"])
        self.assertEqual(0, state["attempt"])
        self.assertEqual(before_contract, contract_hash(workflow.completion_target))  # type: ignore[arg-type]
        self.assertEqual(output["workflow_sha256"], state["workflow_sha256"])
        self.assertNotEqual(
            before_state_hash, sha256_file(self.repo.root / ".cw/state.json")
        )
        validate_state(self.repo.root, state, workflow)
        backup = self.repo.root / output["backup"]
        self.assertEqual(before_workflow, (backup / "phases.yaml").read_bytes())
        self.assertFalse((self.repo.root / TRANSACTION).exists())
        for path in (
            ".cw/runtime/implementer-session.json",
            ".cw/runtime/READY_FOR_REVIEW.json",
            ".cw/runtime/active-run.json",
            ".cw/runtime/batch.json",
        ):
            self.assertFalse((self.repo.root / path).exists())
        self.assertEqual([], list((self.repo.root / ".cw/gates").glob("*.json")))
        self.assertEqual([], list((self.repo.root / ".cw/reviews").glob("*.json")))
        self.assertFalse(
            any(
                (self.repo.root / ".cw/completion" / name).exists()
                for name in ("completion.satisfied.json", "authorizations")
            )
        )
        flags = {phase.id: phase.requires_human_approval for phase in workflow.phases}
        self.assertFalse(flags["01-contracts"])
        self.assertFalse(flags["10-envelope"])
        self.assertTrue(flags["09-read-only"])
        self.assertTrue(flags["11-pilot"])
        self.assertIn(
            "InvoiceInsert", workflow.phases[1].acceptance_criteria[0].description
        )

    def test_json_cli_contract_is_stable_and_agents_are_never_called(self) -> None:
        previous = Path.cwd()
        os.chdir(self.repo.root)
        stream = io.StringIO()
        try:
            with (
                patch("cw.cli.commands.lifecycle.Planner") as planner,
                patch("cw.cli.commands.lifecycle.CodexAdapter") as adapter,
                patch("cw.agents.reviewer.run_review") as reviewer,
                patch("cw.adapters.codex.CodexAdapter.run_implementer") as implementer,
                redirect_stdout(stream),
            ):
                code = main(
                    (
                        "plan",
                        "amend",
                        "--file",
                        "corrected-phases.yaml",
                        "--expected-workflow-sha256",
                        self.repo.sha,
                        "--json",
                    )
                )
        finally:
            os.chdir(previous)
        self.assertEqual(0, code)
        payload = json.loads(stream.getvalue())
        self.assertEqual(
            {
                "amended",
                "status",
                "backup",
                "previous_workflow_sha256",
                "workflow_sha256",
                "phase_count",
                "completion_contract_preserved",
                "approval_required",
            },
            set(payload),
        )
        planner.assert_not_called()
        adapter.assert_not_called()
        reviewer.assert_not_called()
        implementer.assert_not_called()

    def test_human_output_names_backup_hashes_and_separate_approval(self) -> None:
        previous = Path.cwd()
        os.chdir(self.repo.root)
        stream = io.StringIO()
        try:
            with redirect_stdout(stream):
                code = main(
                    (
                        "plan",
                        "amend",
                        "--file",
                        "corrected-phases.yaml",
                        "--expected-workflow-sha256",
                        self.repo.sha,
                    )
                )
        finally:
            os.chdir(previous)
        output = stream.getvalue()
        self.assertEqual(0, code)
        self.assertIn("PLAN_PROPOSED", output)
        self.assertIn("Previous workflow", output)
        self.assertIn("Current workflow", output)
        self.assertIn("Completion Contract", output)
        self.assertIn("cw plan approve", output)

    def test_normal_plan_approve_remains_the_only_following_approval_step(self) -> None:
        self.repo.amend()
        previous = Path.cwd()
        os.chdir(self.repo.root)
        try:
            with redirect_stdout(io.StringIO()):
                code = main(("plan", "approve", "--json"))
        finally:
            os.chdir(previous)
        state = load_state(self.repo.root)
        self.assertEqual(0, code)
        self.assertEqual("READY", state["status"])
        self.assertEqual("APPROVED", load_workflow(self.repo.root).status)
        self.assertIsNotNone(state["active_plan_revision"])
        self.assertEqual([], list((self.repo.root / ".cw/gates").glob("*.json")))
        self.assertEqual([], list((self.repo.root / ".cw/reviews").glob("*.json")))

    def test_plan_rebuild_success_path_remains_available_for_unreviewed_proposal(
        self,
    ) -> None:
        proposed = _read_document(FIXTURES / "moloni-corrected.yaml")
        previous = Path.cwd()
        os.chdir(self.repo.root)
        try:
            with (
                patch("cw.cli.commands.lifecycle.Planner") as planner,
                redirect_stdout(io.StringIO()),
            ):
                planner.return_value.propose_plan.return_value = proposed
                code = main(("plan", "rebuild", "--goal", "Correct proposal", "--json"))
        finally:
            os.chdir(previous)
        self.assertEqual(0, code)
        self.assertEqual("PLAN_PROPOSED", load_state(self.repo.root)["status"])
        self.assertEqual("PROPOSED", load_workflow(self.repo.root).status)
        planner.return_value.propose_plan.assert_called_once()

    def test_stale_sha_fails_without_workflow_or_state_mutation_or_backup(self) -> None:
        before = self.snapshots()
        backups = set((self.repo.root / ".cw/backups").iterdir())
        with self.assertRaises(CwError) as raised:
            amend_plan(self.repo.root, "corrected-phases.yaml", "0" * 64)
        self.assertEqual(ErrorCode.STALE_WORKFLOW_SHA, raised.exception.code)
        self.assertEqual(4, raised.exception.exit_code)
        self.assertEqual(before, self.snapshots())
        self.assertEqual(backups, set((self.repo.root / ".cw/backups").iterdir()))

    def test_raw_and_canonical_sha_formats_apply_the_same_compare_and_swap(
        self,
    ) -> None:
        canonical = self.repo.sha
        raw = canonical.removeprefix("sha256:")
        raw_result = amend_plan(self.repo.root, "corrected-phases.yaml", raw.upper())
        self.assertEqual(canonical, raw_result["previous_workflow_sha256"])
        self.assertRegex(raw_result["workflow_sha256"], r"^sha256:[0-9a-f]{64}$")

        other = ProposedRepo()
        try:
            canonical_result = amend_plan(
                other.root, "corrected-phases.yaml", other.sha
            )
            self.assertEqual(
                raw_result["previous_workflow_sha256"],
                canonical_result["previous_workflow_sha256"],
            )
            self.assertEqual(
                raw_result["workflow_sha256"], canonical_result["workflow_sha256"]
            )
        finally:
            other.close()

    def test_malformed_or_ambiguous_sha_formats_are_rejected_without_mutation(
        self,
    ) -> None:
        before = self.snapshots()
        raw = self.repo.sha.removeprefix("sha256:")
        for value in (
            f"sha512:{raw}",
            raw[:-1],
            raw + "0",
            "g" + raw[1:],
            f" {raw}",
            f"{raw} ",
            "SHA256:" + raw,
        ):
            with self.subTest(value=value), self.assertRaises(CwError) as raised:
                amend_plan(self.repo.root, "corrected-phases.yaml", value)
            self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)
            self.assertEqual(2, raised.exception.exit_code)
            self.assertEqual(before, self.snapshots())

    def test_invalid_yaml_and_schema_fail_without_mutation(self) -> None:
        before = self.snapshots()
        for name, content in (
            ("invalid.yaml", "[:"),
            ("unsafe-tag.yaml", "!!python/object/apply:os.system ['false']\n"),
            ("multiple.yaml", "schema_version: 1\n---\nschema_version: 1\n"),
            ("schema.yaml", '{"schema_version":1}'),
        ):
            (self.repo.root / name).write_text(content, encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(CwError) as raised:
                amend_plan(self.repo.root, name, self.repo.sha)
            self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, raised.exception.code)
            self.assertEqual(2, raised.exception.exit_code)
            self.assertEqual(before, self.snapshots())

    def test_native_yaml_and_json_are_both_supported(self) -> None:
        source = _read_document(FIXTURES / "moloni-corrected.yaml")
        native_yaml = yaml.safe_dump(source, sort_keys=False)
        parsed = workflow_document_from_text(native_yaml)
        self.assertEqual(source, parsed)
        self.assertEqual(source, workflow_document_from_text(json.dumps(source)))
        (self.repo.root / "native-plan.yaml").write_text(native_yaml, encoding="utf-8")
        result = amend_plan(self.repo.root, "native-plan.yaml", self.repo.sha)
        self.assertTrue(result["amended"])
        self.assertEqual(11, result["phase_count"])

    def test_contract_identity_semantics_and_evidence_cannot_change(self) -> None:
        for change in ("id", "target_type", "requirement", "severity", "evidence"):
            document = _read_document(FIXTURES / "moloni-corrected.yaml")
            contract = document["completion_target"]
            if change == "requirement":
                contract["requirements"][0]["id"] = "DIFFERENT"
            elif change == "severity":
                contract["requirements"][0]["severity"] = "advisory"
            elif change == "evidence":
                contract["requirements"][0]["evidence_expectations"] = ["different"]
            else:
                contract[change] = "different"
            name = f"contract-{change}.yaml"
            write_workflow(self.repo.root / name, document)
            before = self.snapshots()
            with self.subTest(change=change), self.assertRaises(CwError) as raised:
                amend_plan(self.repo.root, name, self.repo.sha)
            self.assertEqual(
                ErrorCode.COMPLETION_CONTRACT_CHANGE_REQUIRES_REBUILD,
                raised.exception.code,
            )
            self.assertEqual(before, self.snapshots())

    def test_duplicate_missing_dependency_and_cycle_are_rejected(self) -> None:
        cases = {}
        duplicate = _read_document(FIXTURES / "moloni-corrected.yaml")
        duplicate["phases"][1]["id"] = duplicate["phases"][0]["id"]
        cases["duplicate"] = duplicate
        missing = _read_document(FIXTURES / "moloni-corrected.yaml")
        missing["phases"][1]["depends_on"] = ["99-absent"]
        cases["missing"] = missing
        cycle = _read_document(FIXTURES / "moloni-corrected.yaml")
        cycle["phases"][0]["depends_on"] = ["11-pilot"]
        cases["cycle"] = cycle
        before = self.snapshots()
        for name, document in cases.items():
            write_workflow(self.repo.root / f"{name}.yaml", document)
            with self.subTest(name=name), self.assertRaises(CwError) as raised:
                amend_plan(self.repo.root, f"{name}.yaml", self.repo.sha)
            self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, raised.exception.code)
            self.assertEqual(before, self.snapshots())

    def test_every_non_proposed_state_is_rejected(self) -> None:
        states = [
            state for state in WorkflowState if state is not WorkflowState.PLAN_PROPOSED
        ]
        for status in states:
            state = load_state(self.repo.root)
            state["status"] = status.value
            save_state(self.repo.root, state)
            with (
                self.subTest(status=status.value),
                self.assertRaises(CwError) as raised,
            ):
                amend_plan(self.repo.root, "corrected-phases.yaml", self.repo.sha)
            self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)
            state["status"] = WorkflowState.PLAN_PROPOSED.value
            save_state(self.repo.root, state)

    def test_active_session_readiness_run_batch_gate_and_review_block(self) -> None:
        cases = (
            ".cw/runtime/implementer-session.json",
            ".cw/runtime/READY_FOR_REVIEW.json",
            ".cw/runtime/active-run.json",
            ".cw/runtime/batch.json",
            ".cw/gates/incompatible.json",
            ".cw/reviews/incompatible.json",
            ".cw/completion/reviews/incompatible.json",
            ".cw/completion/proposals/incompatible.json",
            ".cw/completion/authorizations/incompatible.json",
        )
        for relative in cases:
            path = self.repo.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
            with self.subTest(relative=relative), self.assertRaises(CwError):
                amend_plan(self.repo.root, "corrected-phases.yaml", self.repo.sha)
            path.unlink()

    def test_cli_error_exit_codes_are_deterministic(self) -> None:
        previous = Path.cwd()
        os.chdir(self.repo.root)
        try:
            with redirect_stdout(io.StringIO()):
                stale = main(
                    (
                        "plan",
                        "amend",
                        "--file",
                        "corrected-phases.yaml",
                        "--expected-workflow-sha256",
                        "0" * 64,
                        "--json",
                    )
                )
            changed = _read_document(FIXTURES / "moloni-corrected.yaml")
            changed["completion_target"]["id"] = "another-contract"
            write_workflow(self.repo.root / "contract-change.yaml", changed)
            with redirect_stdout(io.StringIO()):
                contract = main(
                    (
                        "plan",
                        "amend",
                        "--file",
                        "contract-change.yaml",
                        "--expected-workflow-sha256",
                        self.repo.sha,
                        "--json",
                    )
                )
            with redirect_stdout(io.StringIO()):
                usage = main(
                    ("plan", "amend", "--file", "corrected-phases.yaml", "--json")
                )
        finally:
            os.chdir(previous)
        self.assertEqual(4, stale)
        self.assertEqual(3, contract)
        self.assertEqual(2, usage)

    def test_simulated_failures_rollback_both_files_byte_exact(self) -> None:
        for stage in (
            "before_workflow_write",
            "after_workflow_write",
            "after_state_write",
        ):
            before = self.snapshots()

            def fail(current: str, expected: str = stage) -> None:
                if current == expected:
                    raise RuntimeError("simulated")

            with self.subTest(stage=stage), self.assertRaises(CwError) as raised:
                self.repo.amend(failure_injector=fail)
            self.assertEqual(
                ErrorCode.PLAN_AMEND_INTEGRITY_ERROR, raised.exception.code
            )
            self.assertEqual(before, self.snapshots())
            self.assertFalse((self.repo.root / TRANSACTION).exists())

    def test_interrupted_transaction_is_recovered_before_a_new_amendment(self) -> None:
        previous_sha = self.repo.sha
        previous_state_sha = sha256_file(self.repo.root / ".cw/state.json")
        backup = backup_metadata(self.repo.root)
        transaction = {
            "kind": "plan_amend",
            "created_at": "2026-08-20T00:00:00Z",
            "backup": backup.relative_to(self.repo.root).as_posix(),
            "previous_workflow_sha256": previous_sha,
            "workflow_sha256": "sha256:" + "1" * 64,
            "previous_state_sha256": previous_state_sha,
            "input_sha256": sha256_file(self.repo.root / "corrected-phases.yaml"),
        }
        atomic_json(self.repo.root / TRANSACTION, transaction)
        (self.repo.root / ".codex/workflow/phases.yaml").write_text(
            "{}\n", encoding="utf-8"
        )
        result = amend_plan(self.repo.root, "corrected-phases.yaml", previous_sha)
        self.assertTrue(result["amended"])
        self.assertFalse((self.repo.root / TRANSACTION).exists())

    def test_rollback_failure_has_a_deterministic_error_code(self) -> None:
        def corrupt_backup(stage: str) -> None:
            if stage != "after_workflow_write":
                return
            transaction = json.loads(
                (self.repo.root / TRANSACTION).read_text(encoding="utf-8")
            )
            (self.repo.root / transaction["backup"] / "phases.yaml").write_bytes(
                b"corrupt"
            )
            raise RuntimeError("simulated")

        with self.assertRaises(CwError) as raised:
            self.repo.amend(failure_injector=corrupt_backup)
        self.assertEqual(ErrorCode.PLAN_AMEND_ROLLBACK_FAILED, raised.exception.code)

    def test_unrelated_historical_evidence_is_preserved(self) -> None:
        historical = self.repo.root / ".cw/backups/historical/reviews/old.json"
        historical.parent.mkdir(parents=True)
        historical.write_bytes(b'{"immutable":true}\n')
        digest = sha256_file(historical)
        archived = self.repo.root / ".cw/reviews/archive/old.json"
        archived.parent.mkdir(parents=True)
        archived.write_bytes(b'{"archived":true}\n')
        archived_digest = sha256_file(archived)
        self.repo.amend()
        self.assertEqual(digest, sha256_file(historical))
        self.assertEqual(archived_digest, sha256_file(archived))

    def test_workflow_without_completion_contract_can_be_amended_without_inventing_one(
        self,
    ) -> None:
        current = _read_document(FIXTURES / "moloni-incomplete.yaml")
        proposed = _read_document(FIXTURES / "moloni-corrected.yaml")
        current.pop("completion_target")
        proposed.pop("completion_target")
        write_workflow(self.repo.root / ".codex/workflow/phases.yaml", current)
        state = load_state(self.repo.root)
        state["workflow_sha256"] = workflow_hash(
            self.repo.root / ".codex/workflow/phases.yaml"
        )
        save_state(self.repo.root, state)
        write_workflow(self.repo.root / "no-contract.yaml", proposed)
        result = amend_plan(self.repo.root, "no-contract.yaml", self.repo.sha)
        self.assertTrue(result["completion_contract_preserved"])
        self.assertIsNone(load_workflow(self.repo.root).completion_target)

    def test_input_must_be_repository_relative_regular_and_outside_metadata(
        self,
    ) -> None:
        directory = self.repo.root / "directory-input"
        directory.mkdir()
        for value in (
            str((self.repo.root / "corrected-phases.yaml").resolve()),
            ".codex/workflow/phases.yaml",
            "directory-input",
        ):
            with self.subTest(value=value), self.assertRaises(CwError) as raised:
                amend_plan(self.repo.root, value, self.repo.sha)
            self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)

    def test_symlink_input_is_rejected_even_when_platform_creation_is_unavailable(
        self,
    ) -> None:
        with (
            patch("cw.core.plan_amendment.Path.is_symlink", return_value=True),
            self.assertRaises(CwError) as raised,
        ):
            amend_plan(self.repo.root, "corrected-phases.yaml", self.repo.sha)
        self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)

    def test_input_replaced_between_validation_and_open_fails_without_mutation(
        self,
    ) -> None:
        before = self.snapshots()
        backups = set((self.repo.root / ".cw/backups").iterdir())
        target = self.repo.root / "corrected-phases.yaml"
        replacement = self.repo.root / "replacement.yaml"
        replacement.write_bytes(target.read_bytes())
        real_open = os.open
        swapped = False

        def replace_then_open(path, flags):
            nonlocal swapped
            if not swapped and Path(path) == target:
                os.replace(replacement, target)
                swapped = True
            return real_open(path, flags)

        with (
            patch("cw.core.plan_amendment.os.open", side_effect=replace_then_open),
            self.assertRaises(CwError) as raised,
        ):
            amend_plan(self.repo.root, "corrected-phases.yaml", self.repo.sha)
        self.assertEqual(ErrorCode.USAGE_ERROR, raised.exception.code)
        self.assertEqual(before, self.snapshots())
        self.assertEqual(backups, set((self.repo.root / ".cw/backups").iterdir()))

    def test_amendment_history_is_auditable_and_completion_cycle_does_not_advance(
        self,
    ) -> None:
        state = load_state(self.repo.root)
        state["completion_cycle"] = 7
        save_state(self.repo.root, state)
        output = self.repo.amend()
        state = load_state(self.repo.root)
        event = state["history"][-1]
        self.assertEqual(7, state["completion_cycle"])
        self.assertEqual("plan_amended", event["action"])
        self.assertEqual(
            output["previous_workflow_sha256"], event["previous_workflow_sha256"]
        )
        self.assertEqual(output["workflow_sha256"], event["workflow_sha256"])
        self.assertEqual(output["backup"], event["backup"])

    def test_ambiguous_json_yaml_aliases_and_oversized_payload_fail_closed(self) -> None:
        target = self.repo.root / "corrected-phases.yaml"
        for content in (
            '{"schema_version":1,"schema_version":1}',
            "schema_version: &version 1\nworkflow: {id: sample}\ncopy: *version\n",
        ):
            with self.subTest(content=content[:20]):
                target.write_text(content, encoding="utf-8")
                with self.assertRaises(CwError) as raised:
                    amend_plan(self.repo.root, "corrected-phases.yaml", self.repo.sha)
                self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, raised.exception.code)
        target.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(CwError) as oversized:
            amend_plan(self.repo.root, "corrected-phases.yaml", self.repo.sha)
        self.assertEqual(ErrorCode.USAGE_ERROR, oversized.exception.code)


class ActiveArtifactAmendmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ActiveArtifactRepo()

    def tearDown(self) -> None:
        self.case.close()

    def tree(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.case.root).as_posix(): path.read_bytes()
            for path in self.case.root.rglob("*")
            if path.is_file() and not path.is_symlink() and ".git" not in path.parts
        }

    def filesystem_inventory(self) -> tuple[dict[str, tuple[int, int, int, str | None]], str, str, str]:
        inventory: dict[str, tuple[int, int, int, str | None]] = {}
        for path in sorted(self.case.root.rglob("*")):
            if ".git" in path.parts:
                continue
            metadata = path.lstat()
            digest = sha256_file(path) if path.is_file() and not path.is_symlink() else None
            inventory[path.relative_to(self.case.root).as_posix()] = (
                stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
                metadata.st_mtime_ns,
                digest,
            )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=self.case.root,
            text=True, capture_output=True, check=True,
        ).stdout
        head_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=self.case.root,
            text=True, capture_output=True, check=False,
        )
        head = head_result.stdout.strip() if head_result.returncode == 0 else "UNBORN"
        index = subprocess.run(
            ["git", "ls-files", "--stage"], cwd=self.case.root,
            text=True, capture_output=True, check=True,
        ).stdout
        return inventory, status, head, index

    def test_generic_active_amendment_preserves_contract_and_supersedes_evidence(self) -> None:
        before_contract = contract_hash(load_workflow(self.case.root).completion_target) if load_workflow(self.case.root).completion_target else None
        original = {
            path.relative_to(self.case.root).as_posix(): path.read_bytes()
            for path in [
                self.case.root / ".cw/runtime/READY_FOR_REVIEW.json",
                *sorted((self.case.root / ".cw/reviews").glob("*.json")),
                *sorted((self.case.root / ".cw/validation").glob("*.json")),
            ]
        }
        output = self.case.apply()
        workflow = load_workflow(self.case.root)
        state = load_state(self.case.root)
        self.assertEqual([self.case.addition], output["added_artifacts"])
        self.assertEqual([], output["removed_artifacts"])
        self.assertEqual([], output["other_changes"])
        self.assertEqual("PLAN_PROPOSED", state["status"])
        self.assertEqual("PROPOSED", workflow.status)
        self.assertFalse(output["automatic_approval"])
        self.assertEqual(before_contract, contract_hash(workflow.completion_target) if workflow.completion_target else None)
        self.assertIn(self.case.addition, workflow.phase(self.case.phase_id).artifacts)
        self.assertFalse((self.case.root / ".cw/runtime/READY_FOR_REVIEW.json").exists())
        self.assertEqual([], list((self.case.root / ".cw/reviews").glob("*.json")))
        backup = self.case.root / output["backup"]
        for reference, content in original.items():
            self.assertEqual(content, (backup / reference.removeprefix(".cw/")).read_bytes())
        self.assertEqual(5, audit_evidence_supersessions(self.case.root, workflow, state))
        self.assertEqual(5, len(list((self.case.root / ".cw/evidence-supersessions").glob("*.json"))))
        validate_state(self.case.root, state, workflow)

    def test_legacy_missing_supersessions_dry_run_is_mutation_free_and_apply_creates_it(self) -> None:
        self.case.close()
        self.case = ActiveArtifactRepo(
            legacy_missing_supersessions=True,
            legacy_missing_plan_revisions=True,
        )
        supersessions = self.case.root / ".cw/supersessions"
        plan_revisions = self.case.root / ".cw/plan-revisions"
        before = self.tree()
        output = prepare_active_artifact_amendment(
            self.case.root, self.case.phase_id, [self.case.addition],
            self.case.workflow_sha, self.case.state_sha,
            "Declare an existing omitted contract artifact",
        )
        self.assertEqual(before, self.tree())
        self.assertFalse(supersessions.exists())
        self.assertFalse(plan_revisions.exists())
        self.assertEqual([self.case.addition], output["added_artifacts"])
        self.assertEqual([], output["removed_artifacts"])
        self.assertEqual([], output["other_changes"])
        self.assertTrue(output["completion_contract_preserved"])
        applied = self.case.apply()
        self.assertTrue(supersessions.is_dir())
        self.assertFalse(supersessions.is_symlink())
        self.assertEqual([], list(supersessions.iterdir()))
        self.assertTrue(plan_revisions.is_dir())
        self.assertFalse(plan_revisions.is_symlink())
        self.assertEqual(2, len(list(plan_revisions.iterdir())))
        self.assertEqual("PLAN_PROPOSED", load_state(self.case.root)["status"])
        self.assertFalse(applied["automatic_approval"])

    def test_legacy_read_surfaces_are_byte_timestamp_and_git_exact(self) -> None:
        self.case.close()
        self.case = TempRepo(name="legacy-cw-0141-read-surfaces")
        state = load_state(self.case.root)
        state["created_with_cw_version"] = "0.14.1"
        state["active_plan_revision"] = None
        state["active_plan_revision_sha256"] = None
        state["superseded_plan_revisions"] = []
        save_state(self.case.root, state)
        shutil.rmtree(self.case.root / ".cw/supersessions")
        shutil.rmtree(self.case.root / ".cw/plan-revisions")
        before = self.filesystem_inventory()
        previous = Path.cwd()
        os.chdir(self.case.root)
        try:
            for command in (("status", "--json"), ("history", "--json")):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(0, main(command))
            with (
                patch("cw.cli.commands.read.shutil.which", return_value="/fixture/bin/tool"),
                patch("cw.adapters.codex.CodexAdapter.smoke_test", return_value=None),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, main(("doctor", "--reviewer", "--json")))
            from cw.core.audit import audit_history

            audit_history(self.case.root, load_workflow(self.case.root), load_state(self.case.root))
        finally:
            os.chdir(previous)
        self.assertEqual(before, self.filesystem_inventory())
        self.assertFalse((self.case.root / ".cw/supersessions").exists())
        self.assertFalse((self.case.root / ".cw/plan-revisions").exists())

    def test_legacy_missing_supersessions_rolls_back_to_absence_at_every_boundary(self) -> None:
        stages = (
            "supersession_directory_created", "plan_revision_directory_created", "old_revision_persisted",
            "new_revision_persisted", "operation_record_persisted",
            "supersessions_persisted", "active_evidence_removed",
            "workflow_activated", "state_activated", "audit_completed",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                case = ActiveArtifactRepo(
                    legacy_missing_supersessions=True,
                    legacy_missing_plan_revisions=True,
                )
                before_workflow = (case.root / ".codex/workflow/phases.yaml").read_bytes()
                before_state = (case.root / ".cw/state.json").read_bytes()
                def fail(name: str) -> None:
                    if name == stage:
                        raise RuntimeError(stage)
                try:
                    with self.assertRaises(CwError):
                        case.apply(failure_injector=fail)
                    self.assertFalse((case.root / ".cw/supersessions").exists())
                    self.assertFalse((case.root / ".cw/plan-revisions").exists())
                    self.assertEqual(before_workflow, (case.root / ".codex/workflow/phases.yaml").read_bytes())
                    self.assertEqual(before_state, (case.root / ".cw/state.json").read_bytes())
                    self.assertFalse((case.root / TRANSACTION).exists())
                finally:
                    case.close()

    def test_dry_run_is_byte_exact_mutation_free_and_reports_both_cas_hashes(self) -> None:
        before = self.tree()
        output = prepare_active_artifact_amendment(
            self.case.root, self.case.phase_id, [self.case.addition],
            self.case.workflow_sha, self.case.state_sha,
            "Declare an existing omitted contract artifact",
        )
        self.assertEqual(before, self.tree())
        self.assertTrue(output["dry_run"])
        self.assertEqual(self.case.workflow_sha, output["expected_workflow_sha256"])
        self.assertEqual(self.case.state_sha, output["expected_state_sha256"])
        self.assertEqual("ADD_PHASE_ARTIFACT", output["operation"])

    def test_repeated_additions_are_explicit_and_case_collisions_fail(self) -> None:
        second = self.case.root / "tests/Fixtures/Contracts/second.graphql"
        second.write_text("type Mutation { example: Boolean! }\n", encoding="utf-8")
        output = prepare_active_artifact_amendment(
            self.case.root, self.case.phase_id, [self.case.addition, second.relative_to(self.case.root).as_posix()],
            self.case.workflow_sha, self.case.state_sha, "Declare both contract artifacts",
        )
        self.assertEqual(2, len(output["added_artifacts"]))
        with self.assertRaises(CwError) as raised:
            prepare_active_artifact_amendment(
                self.case.root, self.case.phase_id, [self.case.addition, self.case.addition.upper()],
                self.case.workflow_sha, self.case.state_sha, "Collision",
            )
        self.assertEqual(ErrorCode.INVALID_ARTIFACT, raised.exception.code)

    def test_workflow_and_state_compare_and_swap_fail_separately(self) -> None:
        with self.assertRaises(CwError) as workflow_error:
            prepare_active_artifact_amendment(
                self.case.root, self.case.phase_id, [self.case.addition], "0" * 64,
                self.case.state_sha, "Stale workflow",
            )
        self.assertEqual(ErrorCode.STALE_WORKFLOW_SHA, workflow_error.exception.code)
        with self.assertRaises(CwError) as state_error:
            prepare_active_artifact_amendment(
                self.case.root, self.case.phase_id, [self.case.addition], self.case.workflow_sha,
                "0" * 64, "Stale state",
            )
        self.assertEqual(ErrorCode.STALE_STATE_SHA, state_error.exception.code)

    def test_invalid_paths_phase_and_completed_gate_fail_closed(self) -> None:
        invalid = ("../escape", "/tmp/absolute", "C:/windows", ".cw/state.json", "", "docs\\phase.md")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CwError) as raised:
                prepare_active_artifact_amendment(
                    self.case.root, self.case.phase_id, [value], self.case.workflow_sha,
                    self.case.state_sha, "Reject unsafe path",
                )
            self.assertEqual(ErrorCode.INVALID_ARTIFACT, raised.exception.code)
        with self.assertRaises(CwError):
            prepare_active_artifact_amendment(
                self.case.root, "02-phase-2", [self.case.addition], self.case.workflow_sha,
                self.case.state_sha, "Wrong phase",
            )

    def test_missing_outside_review_path_symlink_and_hardlink_are_rejected(self) -> None:
        outside = self.case.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        symlink = self.case.root / "tests/Fixtures/Contracts/link.graphql"
        hardlink = self.case.root / "tests/Fixtures/Contracts/hard.graphql"
        try:
            symlink.symlink_to(self.case.root / self.case.addition)
            os.link(self.case.root / self.case.addition, hardlink)
        except OSError:
            self.skipTest("links unavailable on this platform")
        for value in ("missing.graphql", "outside.txt", symlink.relative_to(self.case.root).as_posix(), hardlink.relative_to(self.case.root).as_posix()):
            with self.subTest(value=value), self.assertRaises(CwError) as raised:
                prepare_active_artifact_amendment(
                    self.case.root, self.case.phase_id, [value], self.case.workflow_sha,
                    self.case.state_sha, "Reject invalid artifact",
                )
            self.assertEqual(ErrorCode.INVALID_ARTIFACT, raised.exception.code)

    def test_exact_replay_is_idempotent_and_different_payload_conflicts(self) -> None:
        workflow_sha, state_sha = self.case.workflow_sha, self.case.state_sha
        first = apply_active_artifact_amendment(
            self.case.root, self.case.phase_id, [self.case.addition], workflow_sha,
            state_sha, "Declare an existing omitted contract artifact",
        )
        replay = apply_active_artifact_amendment(
            self.case.root, self.case.phase_id, [self.case.addition], workflow_sha,
            state_sha, "Declare an existing omitted contract artifact",
        )
        self.assertFalse(first.get("idempotent_replay", False))
        self.assertTrue(replay["idempotent_replay"])
        with self.assertRaises(CwError) as raised:
            apply_active_artifact_amendment(
                self.case.root, self.case.phase_id, [self.case.addition], workflow_sha,
                state_sha, "Different reason",
            )
        self.assertIn(raised.exception.code, {ErrorCode.STALE_WORKFLOW_SHA, ErrorCode.OPERATION_CONFLICT})

    def test_failures_at_every_write_boundary_restore_original_bytes(self) -> None:
        stages = (
            "old_revision_persisted", "new_revision_persisted", "operation_record_persisted",
            "supersessions_persisted", "active_evidence_removed", "workflow_activated",
            "state_activated", "audit_completed",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                case = ActiveArtifactRepo()
                before_workflow = (case.root / ".codex/workflow/phases.yaml").read_bytes()
                before_state = (case.root / ".cw/state.json").read_bytes()
                before_evidence = {
                    path.relative_to(case.root).as_posix(): path.read_bytes()
                    for path in [
                        case.root / ".cw/runtime/READY_FOR_REVIEW.json",
                        *sorted((case.root / ".cw/reviews").glob("*.json")),
                        *sorted((case.root / ".cw/validation").glob("*.json")),
                    ]
                }
                def fail(name: str) -> None:
                    if name == stage:
                        raise RuntimeError(stage)
                try:
                    with self.assertRaises(CwError):
                        case.apply(failure_injector=fail)
                    self.assertEqual(before_workflow, (case.root / ".codex/workflow/phases.yaml").read_bytes())
                    self.assertEqual(before_state, (case.root / ".cw/state.json").read_bytes())
                    for reference, content in before_evidence.items():
                        self.assertEqual(content, (case.root / reference).read_bytes())
                    self.assertFalse((case.root / TRANSACTION).exists())
                finally:
                    case.close()

    def test_cli_dry_run_and_confirmed_noninteractive_apply(self) -> None:
        previous = Path.cwd()
        os.chdir(self.case.root)
        try:
            dry = io.StringIO()
            with redirect_stdout(dry):
                self.assertEqual(0, main((
                    "plan", "amend", "--phase", self.case.phase_id,
                    "--add-artifact", self.case.addition,
                    "--expected-workflow-sha256", self.case.workflow_sha,
                    "--expected-state-sha256", self.case.state_sha,
                    "--reason", "Declare omitted artifact", "--dry-run", "--json",
                )))
            payload = json.loads(dry.getvalue())
            self.assertTrue(payload["dry_run"])
            applied = io.StringIO()
            with redirect_stdout(applied):
                self.assertEqual(0, main((
                    "plan", "amend", "--phase", self.case.phase_id,
                    "--add-artifact", self.case.addition,
                    "--expected-workflow-sha256", self.case.workflow_sha,
                    "--expected-state-sha256", self.case.state_sha,
                    "--reason", "Declare omitted artifact", "--apply", "--yes",
                    "--non-interactive", "--json",
                )))
            self.assertEqual("PLAN_PROPOSED", json.loads(applied.getvalue())["status"])
        finally:
            os.chdir(previous)

    def test_noninteractive_apply_without_yes_is_rejected_without_mutation(self) -> None:
        before = (
            (self.case.root / ".codex/workflow/phases.yaml").read_bytes(),
            (self.case.root / ".cw/state.json").read_bytes(),
        )
        previous = Path.cwd()
        os.chdir(self.case.root)
        try:
            self.assertEqual(3, main((
                "plan", "amend", "--phase", self.case.phase_id,
                "--add-artifact", self.case.addition,
                "--expected-workflow-sha256", self.case.workflow_sha,
                "--expected-state-sha256", self.case.state_sha,
                "--reason", "Declare omitted artifact", "--apply", "--non-interactive",
            )))
        finally:
            os.chdir(previous)
        self.assertEqual(before, (
            (self.case.root / ".codex/workflow/phases.yaml").read_bytes(),
            (self.case.root / ".cw/state.json").read_bytes(),
        ))

    def test_sanitized_moloni_shaped_regression_has_no_special_behavior(self) -> None:
        self.case.close()
        self.case = ActiveArtifactRepo(
            phase_id="01-provider-contract-baseline-and-billing-intent-adr",
            addition="tests/Fixtures/Contracts/moloni-on-schema-2026-08-21.graphql",
            legacy_missing_supersessions=True,
        )
        before = self.tree()
        preview = prepare_active_artifact_amendment(
            self.case.root, self.case.phase_id, [self.case.addition],
            self.case.workflow_sha, self.case.state_sha,
            "Declare an existing omitted contract artifact",
        )
        self.assertEqual(before, self.tree())
        self.assertFalse((self.case.root / ".cw/supersessions").exists())
        self.assertEqual(1, len(preview["added_artifacts"]))
        output = self.case.apply()
        self.assertEqual(1, len(output["added_artifacts"]))
        self.assertEqual([], output["removed_artifacts"])
        self.assertEqual([], output["other_changes"])
        self.assertTrue(output["completion_contract_preserved"])
        self.assertEqual("PLAN_PROPOSED", load_state(self.case.root)["status"])

    def test_previous_phase_gate_and_review_are_preserved_byte_for_byte(self) -> None:
        repo = TempRepo(name="previous-phase-history", phases=2)
        try:
            repo.artifact(1)
            repo.ready(1)
            run_review(repo.root, repo.workflow, repo.workflow.phases[0], repo.state(), FakeAdapter(result(1)))
            repo.workflow = load_workflow(repo.root)
            repo.artifact(2)
            addition = repo.root / "docs/phase-2-contract.graphql"
            addition.write_text("type Query { preserved: Boolean! }\n", encoding="utf-8")
            gate = repo.root / ".cw/gates/01-phase-1.approved.json"
            review = next((repo.root / ".cw/reviews").glob("01-phase-1-*.json"))
            before = (gate.read_bytes(), review.read_bytes())
            output = apply_active_artifact_amendment(
                repo.root, "02-phase-2", ["docs/phase-2-contract.graphql"],
                workflow_hash(repo.root / ".codex/workflow/phases.yaml"),
                sha256_file(repo.root / ".cw/state.json"),
                "Declare existing Phase 2 contract artifact",
            )
            self.assertEqual(before, (gate.read_bytes(), review.read_bytes()))
            self.assertEqual([], output["removed_artifacts"])
            validate_state(repo.root, load_state(repo.root), load_workflow(repo.root))
        finally:
            repo.close()

    def test_live_session_and_concurrent_operation_lock_are_rejected(self) -> None:
        session = {
            "schema_version": 1,
            "session_id": "0" * 32,
            "workflow": "generic-marketplace-bridge",
            "phase": self.case.phase_id,
            "status": "ACTIVE",
            "started_at": "2026-08-21T12:00:00Z",
            "owner_pid": os.getpid(),
        }
        atomic_json(self.case.root / ".cw/runtime/implementer-session.json", session)
        with self.assertRaises(CwError) as active:
            prepare_active_artifact_amendment(
                self.case.root, self.case.phase_id, [self.case.addition],
                self.case.workflow_sha, self.case.state_sha, "Blocked by live process",
            )
        self.assertEqual(ErrorCode.LOCKED, active.exception.code)
        (self.case.root / ".cw/runtime/implementer-session.json").unlink()
        with operation_lock(self.case.root, "other-operation"):
            with self.assertRaises(CwError) as locked:
                self.case.apply()
        self.assertEqual(ErrorCode.LOCKED, locked.exception.code)

    def test_artifact_toctou_change_aborts_before_journal(self) -> None:
        from cw.core import plan_amendment

        original = plan_amendment._artifact_path
        calls = 0
        def replace_on_second(root: Path, value: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                (root / value).write_text("changed during apply\n", encoding="utf-8")
            return original(root, value)
        workflow_before = (self.case.root / ".codex/workflow/phases.yaml").read_bytes()
        state_before = (self.case.root / ".cw/state.json").read_bytes()
        with patch("cw.core.plan_amendment._artifact_path", side_effect=replace_on_second):
            with self.assertRaises(CwError) as raised:
                self.case.apply()
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)
        self.assertEqual(workflow_before, (self.case.root / ".codex/workflow/phases.yaml").read_bytes())
        self.assertEqual(state_before, (self.case.root / ".cw/state.json").read_bytes())
        self.assertFalse((self.case.root / TRANSACTION).exists())

    def test_command_is_not_registered_in_mcp_or_remote(self) -> None:
        repository = Path(__file__).parents[1]
        for relative in (
            "cw/adapters/mcp/server.py", "cw/adapters/mcp/runtime.py",
            "cw/remote/server.py", "cw/remote/agent.py",
        ):
            text = (repository / relative).read_text(encoding="utf-8")
            self.assertNotIn("cw_plan_amend", text)
            self.assertNotIn("plan.amend", text)

    def test_doctor_and_audit_are_clean_and_no_agent_is_invoked(self) -> None:
        with (
            patch("cw.cli.commands.lifecycle.Planner") as planner,
            patch("cw.cli.commands.lifecycle.CodexAdapter") as adapter,
            patch("cw.agents.reviewer.run_review") as reviewer,
            patch("cw.adapters.codex.CodexAdapter.run_implementer") as implementer,
        ):
            self.case.apply()
        planner.assert_not_called()
        adapter.assert_not_called()
        reviewer.assert_not_called()
        implementer.assert_not_called()
        workflow = load_workflow(self.case.root)
        state = load_state(self.case.root)
        from cw.core.audit import audit_history

        audit_history(self.case.root, workflow, state)
        previous = Path.cwd()
        os.chdir(self.case.root)
        output = io.StringIO()
        try:
            with (
                patch("cw.cli.commands.read.shutil.which", return_value="/fixture/bin/tool"),
                redirect_stdout(output),
            ):
                self.assertEqual(0, main(("doctor", "--json")))
        finally:
            os.chdir(previous)
        self.assertEqual(0, json.loads(output.getvalue())["result"]["errors"])

    def test_new_human_plan_approval_is_required_and_remains_auditable(self) -> None:
        self.case.apply()
        amended_revision = load_state(self.case.root)["active_plan_revision"]
        previous = Path.cwd()
        os.chdir(self.case.root)
        try:
            self.assertEqual(0, main(("plan", "approve", "--json")))
        finally:
            os.chdir(previous)
        workflow = load_workflow(self.case.root)
        state = load_state(self.case.root)
        self.assertEqual("APPROVED", workflow.status)
        self.assertEqual("READY", state["status"])
        self.assertIn(amended_revision, state["superseded_plan_revisions"])
        from cw.core.audit import audit_history

        audit_history(self.case.root, workflow, state)


if __name__ == "__main__":
    unittest.main()
