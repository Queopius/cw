from __future__ import annotations

import io
import json
import os
import shutil
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
from cw.core.state import bind_plan, load_state, save_state, transition, validate_state
from cw.core.utils import atomic_json, sha256_file
from cw.core.workflow import (
    _read_document,
    load_workflow,
    workflow_document_from_text,
    workflow_hash,
    write_workflow,
)

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


if __name__ == "__main__":
    unittest.main()
