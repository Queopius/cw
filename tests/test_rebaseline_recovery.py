from __future__ import annotations

import copy
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cw.agents.reviewer import run_review
from cw.cli.main import main
from cw.core.audit import audit_history
from cw.core.authorization import (
    Actor,
    ActorOrigin,
    OperationContext,
    issue_user_authorization,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import validate_gate
from cw.core.locking import operation_lock
from cw.core.models import WorkflowState
from cw.core.rebaseline_recovery import (
    TRANSACTION,
    _directory_digest,
    _validate_review,
    apply_rebaseline_recovery,
    preview_rebaseline_recovery,
    recover_rebaseline_recovery_transaction,
)
from cw.core.revisions import (
    apply_rebaseline,
    authorization_resource,
    create_rebaseline_proposal,
    supersession_index,
)
from cw.core.state import load_state, save_state, transition, validate_state
from cw.core.utils import atomic_json, sha256_bytes, sha256_file
from cw.core.workflow import _read_document, load_workflow, workflow_hash
from tests.helpers import FakeAdapter, TempRepo, result

SANITIZED_CASE = Path(__file__).parent / "fixtures/rebaseline-recovery-sanitized.json"
BASELINE_0152 = Path(__file__).parent / "fixtures/rebaseline-recovery-v0.15.2-baseline.json"


class RecoveryCase:
    def __init__(self) -> None:
        self.repo = TempRepo(name="rebaseline-recovery", phases=2)
        self.repo.artifact(1)
        self.repo.ready(1)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[0],
            self.repo.state(), FakeAdapter(result(1)),
        )
        self.gate_bytes = (self.repo.root / ".cw/gates/01-phase-1.approved.json").read_bytes()
        self.gate_reference = ".cw/gates/01-phase-1.approved.json"
        self.gate_sha = sha256_file(self.repo.root / self.gate_reference)
        self.repo.artifact(2)
        self.repo.ready(2)
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[1],
            self.repo.state(), FakeAdapter(result(2, "REVISE", "FAIL")),
        )
        state = load_state(self.repo.root)
        self.review_reference = str(state["last_review"])
        self.review_sha = sha256_file(self.repo.root / self.review_reference)
        state["last_error"] = (
            "PROTECTED_PATH_MODIFIED: Semantic review history does not match its decision"
        )
        transition(self.repo.root, state, WorkflowState.ERROR)
        previous = Path.cwd()
        try:
            os.chdir(self.repo.root)
            with redirect_stdout(io.StringIO()):
                assert main(["repair", "--reopen", "02-phase-2", "--json"]) == 0
        finally:
            os.chdir(previous)
        self.workflow_sha = workflow_hash(self.repo.root / ".codex/workflow/phases.yaml")
        self.state_sha = sha256_file(self.repo.root / ".cw/state.json")

    def close(self) -> None:
        self.repo.close()

    def preview(self) -> dict:
        return preview_rebaseline_recovery(
            self.repo.root, "02-phase-2", self.review_reference, self.review_sha,
            self.workflow_sha, self.state_sha, "Expand the active phase contract",
            expected_prior_gate_reference=self.gate_reference,
            expected_prior_gate_sha256=self.gate_sha,
        )

    def apply(self, **kwargs) -> dict:
        return apply_rebaseline_recovery(
            self.repo.root, "02-phase-2", self.review_reference, self.review_sha,
            self.workflow_sha, self.state_sha, "Expand the active phase contract",
            expected_prior_gate_reference=self.gate_reference,
            expected_prior_gate_sha256=self.gate_sha, **kwargs,
        )

    def evidence(self) -> tuple[Path, dict, Path, dict]:
        result_value = self.apply()
        receipt_path = self.repo.root / result_value["recovery_receipt"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        state_path = self.repo.root / ".cw/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return receipt_path, receipt, state_path, state


class RebaselineRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = RecoveryCase()

    def tearDown(self) -> None:
        self.case.close()

    def test_v0152_contract_baseline_records_the_public_dead_end(self) -> None:
        baseline = json.loads(BASELINE_0152.read_text(encoding="utf-8"))
        self.assertEqual("v0.15.2", baseline["source"]["tag"])
        self.assertEqual("9a84eee61c54c06f54b32f474614b19c7cd95a2e", baseline["source"]["peeled_commit"])
        self.assertEqual("IN_PROGRESS", baseline["observed"]["post_reopen_status"])
        self.assertEqual(1, baseline["observed"]["post_reopen_revision_attempt"])
        self.assertEqual("PLAN_REBASELINE_REQUIRED", baseline["observed"]["rebaseline_error"])
        self.assertFalse(baseline["consumer_data"])

    def test_v0152_public_tag_executable_reproduces_the_dead_end(self) -> None:
        completed = subprocess.run(
            [
                os.environ.get("PYTHON", os.sys.executable),
                "scripts/reproduce_rebaseline_recovery_v0152.py",
                "--repository", str(Path(__file__).parents[1]),
            ],
            cwd=Path(__file__).parents[1],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=240,
        )
        observed = json.loads(completed.stdout)
        self.assertEqual("v0.15.2", observed["tag"])
        self.assertTrue(observed["version"].startswith("CW 0.15.2\n"))
        self.assertEqual("IN_PROGRESS", observed["observed"]["post_reopen_status"])
        self.assertIsNone(observed["observed"]["post_reopen_last_review"])
        self.assertEqual("PLAN_REBASELINE_REQUIRED", observed["observed"]["rebaseline_error"])

    def test_reopen_persists_provenance_and_resets_both_attempt_counters(self) -> None:
        state = load_state(self.case.repo.root)
        self.assertEqual("IN_PROGRESS", state["status"])
        self.assertEqual(0, state["attempt"])
        self.assertEqual(0, state["revision_attempt"])
        event = state["history"][-1]
        self.assertEqual("reopened", event["action"])
        self.assertTrue((self.case.repo.root / event["receipt"]).is_file())
        receipt = json.loads((self.case.repo.root / event["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(event["receipt_sha256"], sha256_file(self.case.repo.root / event["receipt"]))
        self.assertEqual("REVISE", receipt["review_decision"])
        self.assertTrue(receipt["active_plan_revision"].startswith("pr-"))
        self.assertTrue(receipt["active_plan_revision_sha256"].startswith("sha256:"))
        self.assertIsNone(state["last_review"])

    def test_joint_live_and_backup_review_tampering_cannot_rebind_legacy_identity(self) -> None:
        state = load_state(self.case.repo.root)
        event = state["history"][-1]
        receipt_path = self.case.repo.root / event["receipt"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        live = self.case.repo.root / self.case.review_reference
        backup = self.case.repo.root / event["backup"] / self.case.review_reference.removeprefix(".cw/")
        for path in (live, backup):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["summary"] = "jointly altered"
            path.write_text(json.dumps(payload), encoding="utf-8")
        receipt["review_sha256"] = sha256_file(live)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.case.review_sha = sha256_file(live)
        self.case.state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as raised:
            self.case.preview()
        self.assertEqual(ErrorCode.PLAN_REBASELINE_REQUIRED, raised.exception.code)

    def test_preview_is_mutation_free_and_reconciles_prior_gate(self) -> None:
        before = {
            path.relative_to(self.case.repo.root).as_posix(): path.read_bytes()
            for path in self.case.repo.root.rglob("*") if path.is_file()
        }
        result_value = self.case.preview()
        after = {
            path.relative_to(self.case.repo.root).as_posix(): path.read_bytes()
            for path in self.case.repo.root.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual("RECOVERY_PREVIEW", result_value["status"])
        self.assertEqual("REVISION_REQUIRED", result_value["resulting_status"])
        self.assertEqual(".cw/gates/01-phase-1.approved.json", result_value["last_gate"])

    def test_legacy_reopen_without_independent_receipt_binding_fails_closed(self) -> None:
        state = load_state(self.case.repo.root)
        receipt = state["history"][-1].pop("receipt")
        state["history"][-1].pop("receipt_sha256")
        (self.case.repo.root / receipt).unlink()
        save_state(self.case.repo.root, state)
        self.case.state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as raised:
            self.case.preview()
        self.assertEqual(ErrorCode.PLAN_REBASELINE_REQUIRED, raised.exception.code)

    def test_public_help_documents_explicit_recovery_guards(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["plan", "rebaseline", "recover", "--help"])
        self.assertEqual(0, raised.exception.code)
        text = output.getvalue()
        for option in (
            "--phase", "--review-ref", "--expected-review-sha256",
            "--expected-workflow-sha256", "--expected-state-sha256",
            "--expected-prior-gate-ref", "--expected-prior-gate-sha256", "--no-prior-gate",
            "--reason", "--dry-run", "--apply",
        ):
            self.assertIn(option, text)

    def test_public_recovery_examples_bind_exactly_one_prior_gate_authority(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        documents = tuple((repository / "docs").rglob("*.md")) + (
            repository / "README.md",
            repository / "CHANGELOG.md",
        )
        examples: list[tuple[Path, list[str]]] = []
        for document in documents:
            if not document.exists():
                continue
            text = document.read_text(encoding="utf-8")
            for block in re.findall(r"```(?:bash|sh)\n(.*?)```", text, flags=re.DOTALL):
                if "cw plan rebaseline recover" not in block:
                    continue
                command = block.replace("\\\n", " ").strip()
                tokens = shlex.split(command)
                reference_count = tokens.count("--expected-prior-gate-ref")
                digest_count = tokens.count("--expected-prior-gate-sha256")
                absence_count = tokens.count("--no-prior-gate")
                gate_mode = reference_count == 1 and digest_count == 1
                absence_mode = absence_count == 1
                self.assertNotEqual(gate_mode, absence_mode, document)
                self.assertEqual(reference_count, digest_count, document)
                self.assertLessEqual(reference_count, 1, document)
                self.assertLessEqual(absence_count, 1, document)
                examples.append((document, tokens))
        self.assertGreaterEqual(len(examples), 2)

        plan_example = next(tokens for path, tokens in examples if path.name == "plan-revisions.md")
        replacements = {
            "--phase": "02-phase-2",
            "--review-ref": self.case.review_reference,
            "--expected-review-sha256": self.case.review_sha,
            "--expected-workflow-sha256": self.case.workflow_sha,
            "--expected-state-sha256": self.case.state_sha,
            "--expected-prior-gate-ref": self.case.gate_reference,
            "--expected-prior-gate-sha256": self.case.gate_sha,
        }
        executable = list(plan_example[1:])
        for option, value in replacements.items():
            executable[executable.index(option) + 1] = value
        before = {
            path.relative_to(self.case.repo.root).as_posix(): path.read_bytes()
            for path in self.case.repo.root.rglob("*") if path.is_file()
        }
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.case.repo.root)
            with redirect_stdout(output):
                exit_code = main(executable)
        finally:
            os.chdir(previous)
        after = {
            path.relative_to(self.case.repo.root).as_posix(): path.read_bytes()
            for path in self.case.repo.root.rglob("*") if path.is_file()
        }
        self.assertEqual(0, exit_code, output.getvalue())
        self.assertNotIn("USAGE_ERROR", output.getvalue())
        self.assertEqual(before, after)

    def test_apply_restores_rebaseline_precondition_without_changing_workflow_or_gate(self) -> None:
        workflow_before = (self.case.repo.root / ".codex/workflow/phases.yaml").read_bytes()
        result_value = self.case.apply()
        state = load_state(self.case.repo.root)
        self.assertEqual("RECOVERED", result_value["status"])
        self.assertEqual("REVISION_REQUIRED", state["status"])
        self.assertEqual(self.case.review_reference, state["last_review"])
        self.assertEqual(".cw/gates/01-phase-1.approved.json", state["last_gate"])
        self.assertEqual(0, state["attempt"])
        self.assertEqual(0, state["revision_attempt"])
        self.assertEqual(workflow_before, (self.case.repo.root / ".codex/workflow/phases.yaml").read_bytes())
        self.assertEqual(self.case.gate_bytes, (self.case.repo.root / ".cw/gates/01-phase-1.approved.json").read_bytes())
        validate_gate(self.case.repo.root, load_workflow(self.case.repo.root), "01-phase-1")
        validate_state(self.case.repo.root, state, load_workflow(self.case.repo.root))
        audit_history(self.case.repo.root, load_workflow(self.case.repo.root), state)
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())
        self.assertTrue((self.case.repo.root / result_value["recovery_receipt"]).is_file())

    def test_recovered_state_can_propose_monotonic_active_contract_expansion(self) -> None:
        self.case.apply()
        workflow = load_workflow(self.case.repo.root)
        state = load_state(self.case.repo.root)
        document = _read_document(self.case.repo.root / ".codex/workflow/phases.yaml")
        proposed = copy.deepcopy(document)
        proposed["workflow"]["version"] += 1
        proposed["phases"][1]["review_paths"].append("src/**/*")
        proposed["phases"][1]["artifacts"].append("src/Marketplaces/Amazon/Inventory/vector.json")
        preview = create_rebaseline_proposal(
            self.case.repo.root, workflow, state, proposed,
            reason="Declare the expanded active contract",
            actor_id="local-operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
        )
        self.assertEqual("02-phase-2", preview["phase"])
        self.assertTrue(preview["proposal_id"].startswith("pp-"))
        self.assertFalse((self.case.repo.root / "src/Marketplaces/Amazon/Inventory/vector.json").exists())

    def test_separately_authorized_rebaseline_apply_is_ready_and_preserves_prior_gate(self) -> None:
        self.case.apply()
        workflow = load_workflow(self.case.repo.root)
        state = load_state(self.case.repo.root)
        document = _read_document(self.case.repo.root / ".codex/workflow/phases.yaml")
        completion_before = copy.deepcopy(document.get("completion"))
        proposed = copy.deepcopy(document)
        proposed["workflow"]["version"] += 1
        proposed["phases"][1]["review_paths"].append("src/**/*")
        future = "src/Marketplaces/Amazon/Inventory/vector.json"
        proposed["phases"][1]["artifacts"].append(future)
        proposal = create_rebaseline_proposal(
            self.case.repo.root, workflow, state, proposed,
            reason="Declare the expanded active contract",
            actor_id="local-operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
        )
        actor = Actor("local-operator", ActorOrigin.HUMAN_CLI, explicit_user_intent=True)
        operation_id = "rebaseline-recovery-apply-1"
        grant = issue_user_authorization(
            action="plan.rebaseline", resource_id=authorization_resource(proposal),
            operation_id=operation_id, actor=actor,
        )
        outcome = apply_rebaseline(
            self.case.repo.root, workflow, load_state(self.case.repo.root),
            proposal["proposal_id"],
            OperationContext(operation_id, actor, "plan.rebaseline", grant),
        )
        final_workflow = load_workflow(self.case.repo.root)
        final_state = load_state(self.case.repo.root)
        final_document = _read_document(self.case.repo.root / ".codex/workflow/phases.yaml")
        self.assertEqual("REBASELINED", outcome["status"])
        self.assertEqual("READY", final_state["status"])
        self.assertEqual("02-phase-2", final_state["current_phase"])
        self.assertIsNone(final_state["last_review"])
        self.assertEqual(".cw/gates/01-phase-1.approved.json", final_state["last_gate"])
        self.assertTrue((self.case.repo.root / final_state["last_gate"]).is_file())
        self.assertFalse((self.case.repo.root / ".cw/gates/02-phase-2.approved.json").exists())
        self.assertFalse((self.case.repo.root / future).exists())
        self.assertEqual(completion_before, final_document.get("completion"))
        self.assertIn(self.case.review_reference, supersession_index(self.case.repo.root))
        validate_state(self.case.repo.root, final_state, final_workflow)
        audit_history(self.case.repo.root, final_workflow, final_state)

    def test_sanitized_marketplace_contract_declares_future_artifact_without_prior_evidence_change(self) -> None:
        fixture = json.loads(SANITIZED_CASE.read_text(encoding="utf-8"))
        self.assertEqual("02-amazon-inventory-vector", fixture["phase"])
        self.assertIn(
            "src/Marketplaces/Amazon/Inventory/OfficialAmazonVector.php",
            fixture["future_artifact"],
        )
        self.assertNotIn(
            fixture["immutable_prior_phase_evidence"],
            fixture["existing_artifacts"],
        )
        self.assertEqual(0, fixture["expected"]["semantic_removals"])
        self.assertFalse(fixture["expected"]["completion_contract_changed"])
        self.assertFalse(fixture["expected"]["automatic_approval"])

    def test_rebaseline_rejects_removals_and_changes_outside_active_phase(self) -> None:
        self.case.apply()
        workflow = load_workflow(self.case.repo.root)
        state = load_state(self.case.repo.root)
        document = _read_document(self.case.repo.root / ".codex/workflow/phases.yaml")
        removed = copy.deepcopy(document)
        removed["phases"][1]["artifacts"] = []
        with self.assertRaises(CwError) as removal:
            create_rebaseline_proposal(
                self.case.repo.root, workflow, state, removed,
                reason="remove", actor_id="operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
            )
        self.assertEqual(ErrorCode.FORBIDDEN_PLAN_CHANGE, removal.exception.code)
        removed_review_path = copy.deepcopy(document)
        removed_review_path["phases"][1]["review_paths"] = []
        with self.assertRaises(CwError) as review_path_removal:
            create_rebaseline_proposal(
                self.case.repo.root, workflow, state, removed_review_path,
                reason="remove review path", actor_id="operator",
                actor_origin=ActorOrigin.HUMAN_CLI.value,
            )
        self.assertEqual(ErrorCode.FORBIDDEN_PLAN_CHANGE, review_path_removal.exception.code)
        changed_previous = copy.deepcopy(document)
        changed_previous["phases"][0]["objective"] = "tampered"
        with self.assertRaises(CwError) as prior:
            create_rebaseline_proposal(
                self.case.repo.root, workflow, state, changed_previous,
                reason="change prior", actor_id="operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
            )
        self.assertEqual(ErrorCode.INVALID_GATE, prior.exception.code)
        outside = copy.deepcopy(document)
        outside["phases"][1]["artifacts"].append("src/outside.py")
        with self.assertRaises(CwError) as uncovered:
            create_rebaseline_proposal(
                self.case.repo.root, workflow, state, outside,
                reason="outside", actor_id="operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
            )
        self.assertEqual(ErrorCode.INVALID_ARTIFACT, uncovered.exception.code)

    def test_rebaseline_rejects_phase_addition_deletion_and_reordering(self) -> None:
        self.case.apply()
        workflow = load_workflow(self.case.repo.root)
        state = load_state(self.case.repo.root)
        document = _read_document(self.case.repo.root / ".codex/workflow/phases.yaml")
        variants = {
            "addition": [*document["phases"], copy.deepcopy(document["phases"][-1])],
            "deletion": document["phases"][:-1],
            "reordering": list(reversed(document["phases"])),
        }
        variants["addition"][-1]["id"] = "03-added"
        for label, phases in variants.items():
            with self.subTest(label=label):
                proposed = copy.deepcopy(document)
                proposed["phases"] = phases
                with self.assertRaises(CwError):
                    create_rebaseline_proposal(
                        self.case.repo.root, workflow, state, proposed,
                        reason=label, actor_id="operator",
                        actor_origin=ActorOrigin.HUMAN_CLI.value,
                    )

    def test_wrong_review_digest_and_state_cas_fail_closed(self) -> None:
        with self.assertRaises(CwError) as review_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                "sha256:" + "0" * 64, self.case.workflow_sha, self.case.state_sha, "reason",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, review_error.exception.code)
        with self.assertRaises(CwError) as state_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, "sha256:" + "0" * 64, "reason",
            )
        self.assertEqual(ErrorCode.STALE_STATE_SHA, state_error.exception.code)

    def test_workflow_cas_attempts_and_missing_provenance_fail_closed(self) -> None:
        with self.assertRaises(CwError) as workflow_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, "sha256:" + "0" * 64, self.case.state_sha, "reason",
            )
        self.assertEqual(ErrorCode.STALE_WORKFLOW_SHA, workflow_error.exception.code)
        state = load_state(self.case.repo.root)
        state["attempt"] = 1
        save_state(self.case.repo.root, state)
        state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as attempt_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, state_sha, "reason",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, attempt_error.exception.code)
        state["attempt"] = 0
        state["revision_attempt"] = 1
        save_state(self.case.repo.root, state)
        state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as revision_attempt_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, state_sha, "reason",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, revision_attempt_error.exception.code)
        state["revision_attempt"] = 0
        state["history"] = [event for event in state["history"] if event.get("action") != "reopened"]
        save_state(self.case.repo.root, state)
        state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as provenance_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, state_sha, "reason",
            )
        self.assertEqual(ErrorCode.PLAN_REBASELINE_REQUIRED, provenance_error.exception.code)

    def test_active_readiness_gate_and_operation_lock_fail_closed(self) -> None:
        readiness = self.case.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        readiness.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as readiness_error:
            self.case.preview()
        self.assertEqual(ErrorCode.LOCKED, readiness_error.exception.code)
        readiness.unlink()
        gate = self.case.repo.root / ".cw/gates/02-phase-2.approved.json"
        gate.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as gate_error:
            self.case.preview()
        self.assertEqual(ErrorCode.INVALID_GATE, gate_error.exception.code)
        gate.unlink()
        with operation_lock(self.case.repo.root, "other-operation"), self.assertRaises(CwError) as lock_error:
            self.case.preview()
        self.assertEqual(ErrorCode.LOCKED, lock_error.exception.code)

    def test_symlink_hardlink_and_traversal_review_references_fail_closed(self) -> None:
        original = self.case.repo.root / self.case.review_reference
        symlink = original.with_name("symlink.json")
        symlink.symlink_to(original)
        with self.assertRaises(CwError):
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", symlink.relative_to(self.case.repo.root).as_posix(),
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha, "reason",
            )
        symlink.unlink()
        hardlink = original.with_name("hardlink.json")
        os.link(original, hardlink)
        with self.assertRaises(CwError):
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", hardlink.relative_to(self.case.repo.root).as_posix(),
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha, "reason",
            )
        hardlink.unlink()
        with self.assertRaises(CwError):
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", ".cw/reviews/../../state.json",
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha, "reason",
            )

    def test_exact_replay_is_idempotent_and_changed_payload_conflicts(self) -> None:
        first = self.case.apply()
        second = self.case.apply()
        self.assertEqual(first["recovery_id"], second["recovery_id"])
        self.assertTrue(second["idempotent_replay"])
        with self.assertRaises(CwError) as conflict:
            apply_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha,
                "A different reason",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, conflict.exception.code)

    def test_replay_rejects_every_individually_tampered_receipt_field(self) -> None:
        top_level = (
            "schema_version", "kind", "recovery_id", "operation_id", "correlation_id",
            "created_at", "transition", "provenance", "backup",
            "backup_sha256", "before_state_sha256", "after_state_sha256",
        )
        request_fields = (
            "schema_version", "kind", "workflow", "phase", "review_reference",
            "review_sha256", "workflow_sha256", "state_sha256", "reason",
        )
        provenance_fields = (
            "kind", "backup", "backup_sha256", "backup_state_sha256",
            "receipt", "receipt_sha256",
        )
        for field in top_level:
            with self.subTest(scope="receipt", field=field):
                case = RecoveryCase()
                try:
                    result_value = case.apply()
                    path = case.repo.root / result_value["recovery_receipt"]
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload[field] = "tampered"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(CwError):
                        case.apply()
                finally:
                    case.close()
        for field in request_fields:
            with self.subTest(scope="request", field=field):
                case = RecoveryCase()
                try:
                    result_value = case.apply()
                    path = case.repo.root / result_value["recovery_receipt"]
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["request"][field] = "tampered"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(CwError):
                        case.apply()
                finally:
                    case.close()
        for field in provenance_fields:
            with self.subTest(scope="provenance", field=field):
                case = RecoveryCase()
                try:
                    result_value = case.apply()
                    path = case.repo.root / result_value["recovery_receipt"]
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["provenance"][field] = "tampered"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(CwError):
                        case.apply()
                finally:
                    case.close()

    def test_replay_rejects_added_result_and_recalculated_attacker_digest(self) -> None:
        result_value = self.case.apply()
        path = self.case.repo.root / result_value["recovery_receipt"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result"] = {"status": "APPROVED", "review_reference": ".cw/reviews/forged.json"}
        payload["provenance"]["backup_state_sha256"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CwError):
            self.case.apply()

    def test_recalculated_self_digest_cannot_replace_authorized_reconstruction(self) -> None:
        result_value = self.case.apply()
        path = self.case.repo.root / result_value["recovery_receipt"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["after_state_sha256"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertIn(raised.exception.code, {ErrorCode.INVALID_STATE, ErrorCode.PLAN_REVISION_INVALID})

    def test_joint_receipt_history_tamper(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        receipt["created_at"] = "2099-01-01T00:00:00Z"
        state["history"][-1]["timestamp"] = receipt["created_at"]
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)

    def test_recalculated_legacy_binding_is_not_a_trust_root(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        binding = sha256_bytes(json.dumps(receipt, sort_keys=True).encode())
        receipt["receipt_binding_sha256"] = binding
        state["history"][-1]["receipt_binding_sha256"] = binding
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)

    def test_joint_receipt_history_and_after_state_tamper_is_rejected(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        receipt["transition"]["resulting_status"] = "APPROVED"
        state["status"] = "APPROVED"
        state["history"][-1]["action"] = "approved"
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertIn(raised.exception.code, {ErrorCode.INVALID_STATE, ErrorCode.PLAN_REVISION_INVALID})

    def test_joint_receipt_history_and_backup_tamper_is_rejected(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        backup_state = self.case.repo.root / receipt["backup"] / "state.json"
        backup_payload = json.loads(backup_state.read_text(encoding="utf-8"))
        backup_payload["tampered"] = True
        atomic_json(backup_state, backup_payload)
        receipt["before_state_sha256"] = sha256_file(backup_state)
        receipt["request"]["state_sha256"] = sha256_file(backup_state)
        receipt["backup_sha256"] = _directory_digest(backup_state.parent, "tampered backup")
        state["history"][-1]["tampered_backup"] = True
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertIn(raised.exception.code, {ErrorCode.INVALID_STATE, ErrorCode.PLAN_REVISION_INVALID})

    def test_all_attacker_controlled_digests_recalculated_still_fails(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        backup = self.case.repo.root / receipt["backup"]
        (backup / "attacker-added.json").write_text("{}\n", encoding="utf-8")
        receipt["backup_sha256"] = _directory_digest(backup, "tampered backup")
        receipt["after_state_sha256"] = sha256_bytes(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode(),
        )
        state["history"][-1]["review_sha256"] = receipt["request"]["review_sha256"]
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)

    def test_added_governed_field_is_rejected(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        receipt["authorization_override"] = True
        atomic_json(receipt_path, receipt)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_removed_governed_field_is_rejected(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        del receipt["transition"]
        atomic_json(receipt_path, receipt)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_reordered_receipt_fields_are_rejected(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        reordered = dict(reversed(list(receipt.items())))
        atomic_json(receipt_path, reordered)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)

    def test_reordered_history_events_are_rejected(self) -> None:
        _receipt_path, _receipt, state_path, state = self.case.evidence()
        state["history"][-2], state["history"][-1] = state["history"][-1], state["history"][-2]
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)

    def test_history_event_substitution_is_rejected(self) -> None:
        _receipt_path, _receipt, state_path, state = self.case.evidence()
        state["history"][-1] = copy.deepcopy(state["history"][-2])
        atomic_json(state_path, state)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)

    def test_recovery_id_substitution_is_rejected(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        receipt["recovery_id"] = "rr-" + "0" * 64
        receipt["operation_id"] = receipt["recovery_id"]
        receipt["correlation_id"] = receipt["recovery_id"]
        atomic_json(receipt_path, receipt)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_persisted_request_substitution_is_rejected(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        receipt["request"]["reason"] = "substituted request"
        atomic_json(receipt_path, receipt)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_persisted_request_state_cas_substitution_is_rejected(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        receipt["request"]["state_sha256"] = "sha256:" + "0" * 64
        receipt["before_state_sha256"] = receipt["request"]["state_sha256"]
        atomic_json(receipt_path, receipt)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_exact_authorized_request_replay_is_legitimate(self) -> None:
        first = self.case.apply()
        second = self.case.apply()
        self.assertEqual(first["recovery_id"], second["recovery_id"])
        self.assertTrue(second["idempotent_replay"])

    def test_replay_with_different_human_payload_is_conflict(self) -> None:
        self.case.apply()
        with self.assertRaises(CwError) as raised:
            apply_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha,
                "different human request",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_second_incompatible_apply_on_consumed_state_is_conflict(self) -> None:
        self.case.apply()
        with self.assertRaises(CwError) as raised:
            apply_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha,
                "incompatible second recovery",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_tampering_cannot_produce_approved_state(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        receipt["transition"]["resulting_status"] = "APPROVED"
        state["status"] = "APPROVED"
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_tampering_cannot_create_active_phase_gate(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        forged = ".cw/gates/02-phase-2.approved.json"
        receipt["transition"]["resulting_last_gate"] = forged
        state["last_gate"] = forged
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_tampering_cannot_select_another_review(self) -> None:
        receipt_path, receipt, state_path, state = self.case.evidence()
        receipt["request"]["review_reference"] = ".cw/reviews/forged.json"
        state["last_review"] = receipt["request"]["review_reference"]
        state["history"][-1]["review"] = receipt["request"]["review_reference"]
        atomic_json(receipt_path, receipt)
        atomic_json(state_path, state)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_live_review_change_between_backup_and_apply_fails_closed(self) -> None:
        from cw.core.rebaseline_recovery import _create_recovery_backup as real_backup

        def mutate_after_backup(root: Path, recovery_id: str, review_reference: str) -> Path:
            backup = real_backup(root, recovery_id, review_reference)
            review = root / self.case.review_reference
            payload = json.loads(review.read_text(encoding="utf-8"))
            payload["summary"] = "tampered during recovery"
            review.write_text(json.dumps(payload), encoding="utf-8")
            return backup

        with (
            mock.patch(
                "cw.core.rebaseline_recovery._create_recovery_backup",
                side_effect=mutate_after_backup,
            ),
            self.assertRaises(CwError) as raised,
        ):
            self.case.apply()
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_non_revise_cross_phase_and_existing_gate_fail_closed(self) -> None:
        review_path = self.case.repo.root / self.case.review_reference
        original = review_path.read_bytes()
        payload = json.loads(original)
        payload["decision"] = "APPROVE"
        review_path.write_text(json.dumps(payload), encoding="utf-8")
        changed = sha256_file(review_path)
        with self.assertRaises(CwError) as decision_error:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference, changed,
                self.case.workflow_sha, self.case.state_sha, "reason",
            )
        self.assertEqual(ErrorCode.PLAN_REBASELINE_REQUIRED, decision_error.exception.code)

    def test_review_phase_workflow_revision_and_ambiguity_fail_closed(self) -> None:
        for field, value in (
            ("phase", "01-phase-1"),
            ("workflow", "another-workflow"),
            ("plan_revision_id", "pr-" + "0" * 64),
        ):
            with self.subTest(field=field):
                case = RecoveryCase()
                try:
                    path = case.repo.root / case.review_reference
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload[field] = value
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(CwError) as raised:
                        preview_rebaseline_recovery(
                            case.repo.root, "02-phase-2", case.review_reference,
                            sha256_file(path), case.workflow_sha, case.state_sha, "reason",
                        )
                    self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)
                finally:
                    case.close()
        duplicate = (self.case.repo.root / self.case.review_reference).with_name("duplicate.json")
        shutil.copy2(self.case.repo.root / self.case.review_reference, duplicate)
        with self.assertRaises(CwError) as ambiguity:
            self.case.preview()
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, ambiguity.exception.code)

    def test_superseded_review_is_rejected_directly(self) -> None:
        old_workflow = load_workflow(self.case.repo.root)
        self.case.apply()
        state = load_state(self.case.repo.root)
        document = _read_document(self.case.repo.root / ".codex/workflow/phases.yaml")
        proposed = copy.deepcopy(document)
        proposed["workflow"]["version"] += 1
        proposed["phases"][1]["review_paths"].append("src/**/*")
        proposed["phases"][1]["artifacts"].append("src/future.json")
        proposal = create_rebaseline_proposal(
            self.case.repo.root, old_workflow, state, proposed,
            reason="supersede", actor_id="operator", actor_origin=ActorOrigin.HUMAN_CLI.value,
        )
        actor = Actor("operator", ActorOrigin.HUMAN_CLI, explicit_user_intent=True)
        operation_id = "supersession-test"
        grant = issue_user_authorization(
            action="plan.rebaseline", resource_id=authorization_resource(proposal),
            operation_id=operation_id, actor=actor,
        )
        apply_rebaseline(
            self.case.repo.root, old_workflow, load_state(self.case.repo.root),
            proposal["proposal_id"],
            OperationContext(operation_id, actor, "plan.rebaseline", grant),
        )
        with self.assertRaises(CwError) as raised:
            _validate_review(
                self.case.repo.root, old_workflow, load_state(self.case.repo.root),
                "02-phase-2", self.case.review_reference, self.case.review_sha,
            )
        self.assertEqual(ErrorCode.SUPERSESSION_INVALID, raised.exception.code)

    def test_pending_transaction_later_work_and_malformed_receipt_namespace_fail_closed(self) -> None:
        transaction = self.case.repo.root / TRANSACTION
        transaction.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as pending:
            self.case.preview()
        self.assertEqual(ErrorCode.TRANSACTION_RECOVERY_REQUIRED, pending.exception.code)
        transaction.unlink()

        state = load_state(self.case.repo.root)
        state["history"].append({
            "timestamp": "2026-08-24T00:00:00Z",
            "phase": "02-phase-2",
            "action": "implementation_started",
        })
        save_state(self.case.repo.root, state)
        state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as later:
            preview_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, state_sha, "reason",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, later.exception.code)

        case = RecoveryCase()
        try:
            unexpected = case.repo.root / ".cw/rebaseline-recoveries/unexpected.txt"
            unexpected.write_text("unexpected\n", encoding="utf-8")
            with self.assertRaises(CwError) as malformed:
                case.preview()
            self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, malformed.exception.code)
        finally:
            case.close()

    def test_active_session_and_managed_run_fail_closed(self) -> None:
        session = self.case.repo.root / ".cw/runtime/implementer-session.json"
        session.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as active_session:
            self.case.preview()
        self.assertEqual(ErrorCode.LOCKED, active_session.exception.code)
        session.unlink()
        active_run = self.case.repo.root / ".cw/runtime/active-run.json"
        active_run.write_text(json.dumps({
            "schema_version": 1,
            "run_id": "run_" + "0" * 32,
        }), encoding="utf-8")
        with self.assertRaises(CwError) as managed_run:
            self.case.preview()
        self.assertEqual(ErrorCode.LOCKED, managed_run.exception.code)

    def test_already_approved_phase_state_fails_closed(self) -> None:
        state = load_state(self.case.repo.root)
        state["status"] = WorkflowState.COMPLETED.value
        save_state(self.case.repo.root, state)
        self.case.state_sha = sha256_file(self.case.repo.root / ".cw/state.json")
        with self.assertRaises(CwError) as raised:
            self.case.preview()
        self.assertEqual(ErrorCode.INVALID_STATE, raised.exception.code)

    def test_backup_inventory_is_bound_and_any_change_breaks_replay(self) -> None:
        result_value = self.case.apply()
        receipt = json.loads(
            (self.case.repo.root / result_value["recovery_receipt"]).read_text(encoding="utf-8")
        )
        backup = self.case.repo.root / receipt["backup"]
        state_backup = backup / "state.json"
        state_backup.write_bytes(state_backup.read_bytes() + b" ")
        with self.assertRaises(CwError):
            self.case.apply()

    def test_doctor_and_history_accept_the_recovered_state(self) -> None:
        self.case.apply()
        previous = Path.cwd()
        try:
            os.chdir(self.case.repo.root)
            for command in (["doctor", "--json"], ["history", "--json"]):
                with self.subTest(command=command), redirect_stdout(io.StringIO()):
                    self.assertEqual(0, main(command))
        finally:
            os.chdir(previous)

    def test_0141_to_0170_rollback_and_reupdate_preserve_project_evidence(self) -> None:
        from tests.test_update import UpdateFixture

        with tempfile.TemporaryDirectory(prefix="cw-update-0141-0170-") as name:
            fixture = UpdateFixture(Path(name), current="0.14.1", target="0.17.0")
            evidence = fixture.base / "consumer/.cw/state.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(b'{"legacy":true}\n')
            before = evidence.read_bytes()
            fixture.service.install()
            self.assertEqual("0.17.0", fixture.installation.active_version())
            self.assertEqual(before, evidence.read_bytes())
            self.assertEqual("0.14.1", fixture.service.rollback().current)
            self.assertEqual(before, evidence.read_bytes())
            fixture.service.install(requested_version="0.17.0")
            self.assertEqual("0.17.0", fixture.installation.active_version())
            self.assertEqual(before, evidence.read_bytes())

    def test_preview_and_apply_do_not_modify_git_index_or_worktree_payload(self) -> None:
        def index() -> bytes:
            return subprocess.run(
                ["git", "diff", "--cached", "--binary"],
                cwd=self.case.repo.root, check=True, capture_output=True,
            ).stdout

        def consumer_payload_status() -> tuple[bytes, ...]:
            raw = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=self.case.repo.root, check=True, capture_output=True,
            ).stdout
            return tuple(
                line for line in raw.splitlines()
                if not line[3:].startswith((b".cw/", b".codex/"))
            )

        before_index = index()
        before_payload = consumer_payload_status()
        self.case.preview()
        self.assertEqual(before_index, index())
        self.assertEqual(before_payload, consumer_payload_status())
        self.case.apply()
        self.assertEqual(before_index, index())
        self.assertEqual(before_payload, consumer_payload_status())

    def test_cli_apply_reports_mutation_and_receipt_in_versioned_output(self) -> None:
        args = [
            "plan", "rebaseline", "recover", "--phase", "02-phase-2",
            "--review-ref", self.case.review_reference,
            "--expected-review-sha256", self.case.review_sha,
            "--expected-workflow-sha256", self.case.workflow_sha,
            "--expected-state-sha256", self.case.state_sha,
            "--expected-prior-gate-ref", self.case.gate_reference,
            "--expected-prior-gate-sha256", self.case.gate_sha,
            "--reason", "Expand the active phase contract", "--apply", "--llm",
        ]
        previous = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.case.repo.root)
            with redirect_stdout(output):
                self.assertEqual(0, main(args))
        finally:
            os.chdir(previous)
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["changed"])
        self.assertTrue(envelope["operation_id"].startswith("rr-"))
        self.assertTrue(envelope["data"]["backup"].startswith(".cw/backups/"))
        self.assertTrue(envelope["data"]["recovery_receipt"].startswith(".cw/rebaseline-recoveries/"))

    def test_failure_injection_rolls_back_every_write_boundary(self) -> None:
        for boundary in (
            "journal_persisted", "receipt_directory_ready", "state_persisted", "receipt_persisted",
        ):
            with self.subTest(boundary=boundary):
                case = RecoveryCase()
                try:
                    original_state = (case.repo.root / ".cw/state.json").read_bytes()

                    def fail(step: str, target: str = boundary) -> None:
                        if step == target:
                            raise RuntimeError("injected")

                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        case.apply(failure_injector=fail)
                    self.assertEqual(original_state, (case.repo.root / ".cw/state.json").read_bytes())
                    self.assertFalse((case.repo.root / TRANSACTION).exists())
                finally:
                    case.close()

    def test_failure_rollback_restores_absent_receipt_namespace(self) -> None:
        directory = self.case.repo.root / ".cw/rebaseline-recoveries"
        directory.rmdir()
        original_state = (self.case.repo.root / ".cw/state.json").read_bytes()

        def fail(step: str) -> None:
            if step == "state_persisted":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.case.apply(failure_injector=fail)
        self.assertFalse(directory.exists())
        self.assertEqual(original_state, (self.case.repo.root / ".cw/state.json").read_bytes())
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())

    def test_failure_after_commit_preserves_commit_for_idempotent_replay(self) -> None:
        def fail(step: str) -> None:
            if step == "committed":
                raise RuntimeError("post-commit disconnect")

        with self.assertRaisesRegex(RuntimeError, "post-commit disconnect"):
            self.case.apply(failure_injector=fail)
        self.assertEqual("REVISION_REQUIRED", load_state(self.case.repo.root)["status"])
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())
        replay = self.case.apply()
        self.assertTrue(replay["idempotent_replay"])

    def test_transaction_crash_recovery_restores_original_state(self) -> None:
        original_state = load_state(self.case.repo.root)
        receipt = ".cw/rebaseline-recoveries/rr-" + "0" * 64 + ".json"
        directory = self.case.repo.root / ".cw/rebaseline-recoveries"
        directory.mkdir(exist_ok=True)
        (self.case.repo.root / ".cw/backups/fixture").mkdir()
        transaction = self.case.repo.root / TRANSACTION
        transaction.write_text(json.dumps({
            "schema_version": 1,
            "kind": "rebaseline_recovery_transaction",
            "status": "PREPARED",
            "recovery_id": "rr-" + "0" * 64,
            "old_state": original_state,
            "backup": ".cw/backups/fixture",
            "backup_sha256": None,
            "receipt": receipt,
            "created_directory": False,
        }), encoding="utf-8")
        state = copy.deepcopy(original_state)
        state["status"] = "REVISION_REQUIRED"
        save_state(self.case.repo.root, state)
        outcome = recover_rebaseline_recovery_transaction(self.case.repo.root)
        assert outcome is not None
        self.assertTrue(outcome["recovered"])
        restored = load_state(self.case.repo.root)
        for key in original_state:
            if key not in {"cw_version", "updated_at"}:
                self.assertEqual(original_state[key], restored[key])

    def test_cli_json_and_output_protocol(self) -> None:
        args = [
            "plan", "rebaseline", "recover", "--phase", "02-phase-2",
            "--review-ref", self.case.review_reference,
            "--expected-review-sha256", self.case.review_sha,
            "--expected-workflow-sha256", self.case.workflow_sha,
            "--expected-state-sha256", self.case.state_sha,
            "--expected-prior-gate-ref", self.case.gate_reference,
            "--expected-prior-gate-sha256", self.case.gate_sha,
            "--reason", "Expand the active phase contract", "--dry-run", "--output=json",
        ]
        previous = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.case.repo.root)
            with redirect_stdout(output):
                self.assertEqual(0, main(args))
        finally:
            os.chdir(previous)
        envelope = json.loads(output.getvalue())
        self.assertEqual("cw.output.v1", envelope["schema"])
        self.assertEqual("plan.rebaseline.recover", envelope["command"])
        self.assertEqual("RECOVERY_PREVIEW", envelope["data"]["status"])
        self.assertFalse(envelope["changed"])
        self.assertEqual(envelope["operation_id"], envelope["data"]["operation_id"])

        args[-1] = "--llm"
        output = io.StringIO()
        try:
            os.chdir(self.case.repo.root)
            with redirect_stdout(output):
                self.assertEqual(0, main(args))
        finally:
            os.chdir(previous)
        compact = json.loads(output.getvalue())
        self.assertFalse(compact["changed"])
        self.assertTrue(compact["operation_id"].startswith("rr-"))
        self.assertEqual(self.case.review_reference, compact["data"]["review_reference"])
        self.assertEqual(self.case.review_sha, compact["data"]["review_sha256"])
        self.assertEqual(self.case.workflow_sha, compact["data"]["workflow_sha256"])
        self.assertEqual(self.case.state_sha, compact["data"]["state_sha256"])
        self.assertEqual("IN_PROGRESS", compact["data"]["previous_status"])
        self.assertEqual("REVISION_REQUIRED", compact["data"]["resulting_status"])
        self.assertIn("last_gate", compact["data"])
        self.assertIn("next_action", compact["data"])
        self.assertNotIn("status", compact["data"])


if __name__ == "__main__":
    unittest.main()
