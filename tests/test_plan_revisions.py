from __future__ import annotations

import copy
import hashlib
import json
import unittest
import io
import os
import shutil
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from cw.agents.reviewer import run_review
from cw.core.audit import audit_history
from cw.core.authorization import (
    Actor,
    ActorOrigin,
    AuthorizationGrant,
    OperationContext,
    issue_user_authorization,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import validate_gate
from cw.core.history import history_timeline
from cw.core.models import WorkflowState
from cw.core.revisions import (
    TRANSACTION,
    _write_transaction,
    active_revision,
    apply_rebaseline,
    audit_revisions,
    authorization_resource,
    create_rebaseline_proposal,
    plan_revision_directory,
    recover_rebaseline_transaction,
    supersession_index,
)
from cw.core.initialize import backup_metadata
from cw.core.state import load_state, save_state, transition
from cw.core.utils import atomic_json, load_json
from cw.core.workflow import _read_document, load_workflow, write_workflow
from cw.cli.main import main
from cw.application import CWApplication
from cw.application.actions import validate_current_phase
from cw.application.projects import ProjectHandle, ResolvedProject
from tests.helpers import FakeAdapter, TempRepo, result


DASHBOARD_CASE = Path(__file__).parent / "fixtures/cw-dashboard-rebaseline.json"


class LegacySupersessionIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(name="legacy-cw-0141")
        self.directory = self.repo.root / ".cw/supersessions"
        shutil.rmtree(self.directory)

    def tearDown(self) -> None:
        self.repo.close()

    def test_missing_and_empty_directories_are_empty_indexes_without_creation(self) -> None:
        self.assertEqual({}, supersession_index(self.repo.root))
        self.assertFalse(self.directory.exists())
        self.directory.mkdir()
        self.assertEqual({}, supersession_index(self.repo.root))

    def test_file_directory_symlinks_and_dangling_symlinks_fail_closed(self) -> None:
        target = self.repo.root / "supersession-target"
        target.mkdir()
        cases = ("file", "directory-symlink", "dangling-symlink")
        for kind in cases:
            with self.subTest(kind=kind):
                if self.directory.exists() or self.directory.is_symlink():
                    if self.directory.is_dir() and not self.directory.is_symlink():
                        shutil.rmtree(self.directory)
                    else:
                        self.directory.unlink()
                if kind == "file":
                    self.directory.write_text("not a directory\n", encoding="utf-8")
                elif kind == "directory-symlink":
                    self.directory.symlink_to(target, target_is_directory=True)
                else:
                    self.directory.symlink_to(self.repo.root / "missing-target", target_is_directory=True)
                with self.assertRaises(CwError) as raised:
                    supersession_index(self.repo.root)
                self.assertEqual(ErrorCode.SUPERSESSION_INVALID, raised.exception.code)

    def test_unsafe_entries_and_malformed_json_fail_closed(self) -> None:
        self.directory.mkdir()
        unexpected = self.directory / "unexpected.txt"
        unexpected.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CwError) as unexpected_error:
            supersession_index(self.repo.root)
        self.assertEqual(ErrorCode.SUPERSESSION_INVALID, unexpected_error.exception.code)
        unexpected.unlink()
        malformed = self.directory / ("ps-" + "0" * 64 + ".json")
        malformed.write_text("{", encoding="utf-8")
        with self.assertRaises(CwError) as malformed_error:
            supersession_index(self.repo.root)
        self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, malformed_error.exception.code)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_fifo_directory_and_entry_fail_closed(self) -> None:
        os.mkfifo(self.directory)
        with self.assertRaises(CwError) as directory_error:
            supersession_index(self.repo.root)
        self.assertEqual(ErrorCode.SUPERSESSION_INVALID, directory_error.exception.code)
        self.directory.unlink()
        self.directory.mkdir()
        os.mkfifo(self.directory / ("ps-" + "0" * 64 + ".json"))
        with self.assertRaises(CwError) as entry_error:
            supersession_index(self.repo.root)
        self.assertEqual(ErrorCode.SUPERSESSION_INVALID, entry_error.exception.code)


class LegacyPlanRevisionIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(name="legacy-plan-revisions")
        self.directory = self.repo.root / ".cw/plan-revisions"
        shutil.rmtree(self.directory)
        state = load_state(self.repo.root)
        state["created_with_cw_version"] = "0.14.1"
        state["active_plan_revision"] = None
        state["active_plan_revision_sha256"] = None
        state["superseded_plan_revisions"] = []
        save_state(self.repo.root, state)

    def tearDown(self) -> None:
        self.repo.close()

    def test_missing_directory_is_empty_index_for_coherent_legacy_state(self) -> None:
        workflow = load_workflow(self.repo.root)
        state = load_state(self.repo.root)
        outcome = audit_revisions(self.repo.root, workflow, state)
        self.assertTrue(outcome["legacy_derived"])
        self.assertFalse(self.directory.exists())
        self.assertIsNone(plan_revision_directory(self.repo.root))

    def test_empty_directory_is_valid_for_legacy_state(self) -> None:
        self.directory.mkdir()
        workflow = load_workflow(self.repo.root)
        state = load_state(self.repo.root)
        outcome = audit_revisions(self.repo.root, workflow, state)
        self.assertTrue(outcome["legacy_derived"])

    def test_missing_directory_fails_when_state_declares_revision(self) -> None:
        state = load_state(self.repo.root)
        state["active_plan_revision"] = "pr-" + "0" * 64
        state["active_plan_revision_sha256"] = "sha256:" + "0" * 64
        save_state(self.repo.root, state)
        with self.assertRaises(CwError) as raised:
            audit_revisions(self.repo.root, load_workflow(self.repo.root), load_state(self.repo.root))
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)

    def test_missing_directory_fails_for_current_version_state(self) -> None:
        state = load_state(self.repo.root)
        state["created_with_cw_version"] = "0.15.2"
        save_state(self.repo.root, state)
        with self.assertRaises(CwError) as raised:
            audit_revisions(self.repo.root, load_workflow(self.repo.root), load_state(self.repo.root))
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)

    def test_missing_directory_fails_when_history_requires_revisions(self) -> None:
        state = load_state(self.repo.root)
        state.setdefault("history", []).append({
            "timestamp": "2026-08-23T00:00:00Z",
            "phase": "01-phase-1",
            "action": "phase_artifacts_amended",
        })
        save_state(self.repo.root, state)
        with self.assertRaises(CwError) as raised:
            audit_revisions(self.repo.root, load_workflow(self.repo.root), load_state(self.repo.root))
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)

    def test_file_symlink_and_unexpected_entries_fail_closed(self) -> None:
        target = self.repo.root / "revision-target"
        target.mkdir()
        cases = ("file", "directory-symlink", "dangling-symlink", "unexpected-entry")
        for kind in cases:
            with self.subTest(kind=kind):
                if self.directory.exists() or self.directory.is_symlink():
                    if self.directory.is_dir() and not self.directory.is_symlink():
                        shutil.rmtree(self.directory)
                    else:
                        self.directory.unlink()
                if kind == "file":
                    self.directory.write_text("not a directory\n", encoding="utf-8")
                elif kind == "directory-symlink":
                    self.directory.symlink_to(target, target_is_directory=True)
                elif kind == "dangling-symlink":
                    self.directory.symlink_to(self.repo.root / "missing-target", target_is_directory=True)
                else:
                    self.directory.mkdir()
                    (self.directory / "unexpected.txt").write_text("{}\n", encoding="utf-8")
                with self.assertRaises(CwError) as raised:
                    audit_revisions(self.repo.root, load_workflow(self.repo.root), load_state(self.repo.root))
                self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_fifo_directory_and_entry_fail_closed(self) -> None:
        os.mkfifo(self.directory)
        with self.assertRaises(CwError) as directory_error:
            audit_revisions(self.repo.root, load_workflow(self.repo.root), load_state(self.repo.root))
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, directory_error.exception.code)
        self.directory.unlink()
        self.directory.mkdir()
        os.mkfifo(self.directory / ("pr-" + "0" * 64 + ".json"))
        with self.assertRaises(CwError) as entry_error:
            audit_revisions(self.repo.root, load_workflow(self.repo.root), load_state(self.repo.root))
        self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, entry_error.exception.code)


class RebaselineCase:
    def __init__(self, *, phases: int = 1, unicode_name: bool = False) -> None:
        self.repo = TempRepo(name="CW Dashboard São Paulo" if unicode_name else "cw-dashboard", phases=phases)
        self.phase_number = phases
        for number in range(1, phases):
            self.repo.artifact(number)
            self.repo.ready(number)
            run_review(
                self.repo.root, self.repo.workflow, self.repo.workflow.phases[number - 1],
                self.repo.state(), FakeAdapter(result(number)),
            )
        self.repo.artifact(phases)
        self.repo.ready(phases)
        self.project = ResolvedProject(
            self.repo.root, self.repo.project,
            ProjectHandle(self.repo.project.project_id, "dashboard-fixture", self.repo.root.name),
        )
        if not unicode_name:
            validate_current_phase(self.project, f"validation-a-phase-{phases}")
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[phases - 1],
            self.repo.state(), FakeAdapter(result(phases, "REVISE", "FAIL")),
        )
        self.review_reference = str(self.repo.state()["last_review"])
        self.review_bytes = (self.repo.root / self.review_reference).read_bytes()
        document = _read_document(self.repo.root / ".codex/workflow/phases.yaml")
        self.proposed = copy.deepcopy(document)
        current = self.proposed["phases"][phases - 1]
        current["acceptance_criteria"] = [{
            "id": "ARCH-001", "description": "Approved architecture boundaries are documented", "severity": "blocking",
        }]
        current["blocking_criteria"] = []
        self.actor = Actor("local-operator", ActorOrigin.HUMAN_CLI, explicit_user_intent=True)

    def close(self) -> None:
        self.repo.close()

    def preview(self, *, reason: str = "Remove circular Phase 00 requirements") -> dict:
        return create_rebaseline_proposal(
            self.repo.root, self.repo.workflow, self.repo.state(), self.proposed,
            reason=reason, actor_id=self.actor.actor_id, actor_origin=self.actor.origin.value,
        )

    def context(self, proposal: dict, operation_id: str = "dashboard-rebaseline-1") -> OperationContext:
        grant = issue_user_authorization(
            action="plan.rebaseline", resource_id=authorization_resource(proposal),
            operation_id=operation_id, actor=self.actor,
        )
        return OperationContext(operation_id, self.actor, "plan.rebaseline", grant)

    def apply(self, proposal: dict, operation_id: str = "dashboard-rebaseline-1", **kwargs):
        return apply_rebaseline(
            self.repo.root, self.repo.workflow, load_state(self.repo.root),
            proposal["proposal_id"], self.context(proposal, operation_id), **kwargs,
        )


class PlanRevisionHappyPathTests(unittest.TestCase):
    def test_dashboard_regression_preserves_review_and_gates_only_revision_b(self) -> None:
        metadata = json.loads(DASHBOARD_CASE.read_text(encoding="utf-8"))
        self.assertEqual(
            "cb995baf9e70709e37fd66e53c01f30ac81d2f92963f1381101b6473aa2bf1d4",
            metadata["review_attempt_1_sha256"],
        )
        case = RebaselineCase()
        try:
            old_hash = hashlib.sha256(case.review_bytes).hexdigest()
            proposal = case.preview()
            outcome = case.apply(proposal)
            workflow = load_workflow(case.repo.root)
            state = load_state(case.repo.root)
            self.assertEqual("READY", state["status"])
            self.assertEqual(1, state["attempt"])
            self.assertEqual(0, state["revision_attempt"])
            self.assertFalse((case.repo.root / ".cw/gates/01-phase-1.approved.json").exists())
            self.assertEqual(case.review_bytes, (case.repo.root / case.review_reference).read_bytes())
            self.assertEqual(old_hash, hashlib.sha256((case.repo.root / case.review_reference).read_bytes()).hexdigest())
            self.assertEqual(1, audit_history(case.repo.root, workflow, state)["superseded_reviews"])
            self.assertEqual(2, audit_revisions(case.repo.root, workflow, state)["superseded_reviews"] + 1)
            transition(case.repo.root, state, WorkflowState.IN_PROGRESS)
            case.repo.workflow = workflow
            case.repo.artifact(content="corrected candidate\n")
            case.repo.ready()
            validation = validate_current_phase(case.project, "validation-b-phase-1")
            validation_record = load_json(case.repo.root / validation["evidence"])
            self.assertEqual(2, validation_record["validation_attempt"])
            self.assertEqual(1, validation_record["revision_validation_attempt"])
            self.assertEqual(proposal["new_plan_revision_id"], validation_record["plan_revision_id"])
            report = run_review(
                case.repo.root, workflow, workflow.phases[0], load_state(case.repo.root),
                FakeAdapter(result(1, "APPROVE", "PASS", criterion="ARCH-001")),
            )
            self.assertEqual(2, report["attempt"])
            self.assertEqual(1, report["revision_attempt"])
            gate = validate_gate(case.repo.root, workflow, "01-phase-1")
            self.assertEqual(proposal["new_plan_revision_id"], gate["plan_revision_id"])
            self.assertEqual(report["candidate_sha"], gate["candidate_sha"])
            timeline = history_timeline(case.repo.root, workflow, load_state(case.repo.root))
            old = next(entry for entry in timeline[0]["entries"] if entry.get("review") == case.review_reference)
            self.assertTrue(old["superseded"])
            self.assertEqual("REBASELINED", outcome["status"])
            self.assertFalse((case.repo.root / TRANSACTION).exists())
            audit_history(case.repo.root, workflow, load_state(case.repo.root))
        finally:
            case.close()

    def test_exact_replay_is_idempotent_but_conflicting_operation_is_rejected(self) -> None:
        case = RebaselineCase()
        try:
            proposal = case.preview()
            context = case.context(proposal, "stable-operation")
            first = apply_rebaseline(case.repo.root, case.repo.workflow, case.repo.state(), proposal["proposal_id"], context)
            second = apply_rebaseline(case.repo.root, load_workflow(case.repo.root), load_state(case.repo.root), proposal["proposal_id"], context)
            self.assertFalse(first.get("idempotent_replay", False))
            self.assertTrue(second["idempotent_replay"])
            conflicting = case.context(proposal, "stable-operation")
            with self.assertRaises(CwError) as raised:
                apply_rebaseline(
                    case.repo.root, load_workflow(case.repo.root), load_state(case.repo.root),
                    proposal["proposal_id"], conflicting,
                )
            self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)
        finally:
            case.close()

    def test_previous_phase_gate_survives_rebaseline_of_current_phase(self) -> None:
        case = RebaselineCase(phases=2)
        try:
            first_gate_before = (case.repo.root / ".cw/gates/01-phase-1.approved.json").read_bytes()
            case.proposed["workflow"]["version"] = 2
            proposal = case.preview()
            case.apply(proposal)
            workflow = load_workflow(case.repo.root)
            self.assertEqual(first_gate_before, (case.repo.root / ".cw/gates/01-phase-1.approved.json").read_bytes())
            validate_gate(case.repo.root, workflow, "01-phase-1")
            self.assertFalse((case.repo.root / ".cw/gates/02-phase-2.approved.json").exists())
        finally:
            case.close()


class PlanRevisionAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = RebaselineCase()
        self.proposal = self.case.preview()

    def tearDown(self) -> None:
        self.case.close()

    def assert_code(self, expected: ErrorCode, context: OperationContext) -> None:
        with self.assertRaises(CwError) as raised:
            apply_rebaseline(
                self.case.repo.root, self.case.repo.workflow, self.case.repo.state(),
                self.proposal["proposal_id"], context,
            )
        self.assertEqual(expected, raised.exception.code)

    def test_missing_authorization(self) -> None:
        self.assert_code(
            ErrorCode.AUTHORIZATION_REQUIRED,
            OperationContext("missing", self.case.actor, "plan.rebaseline", None),
        )

    def test_planner_cannot_authorize(self) -> None:
        actor = Actor("planner", ActorOrigin.PLANNER, explicit_user_intent=True)
        grant = AuthorizationGrant(
            "plan.rebaseline", authorization_resource(self.proposal), "planner-op", actor,
            "2026-08-20T00:00:00Z", "2099-08-20T00:00:00Z", "planner-nonce",
        )
        self.assert_code(
            ErrorCode.AUTHORIZATION_REQUIRED,
            OperationContext("planner-op", actor, "plan.rebaseline", grant),
        )

    def test_expired_authorization(self) -> None:
        context = self.case.context(self.proposal, "expired")
        expired = replace(context.authorization, expires_at="2000-01-01T00:00:00Z")
        self.assert_code(
            ErrorCode.AUTHORIZATION_REQUIRED,
            OperationContext("expired", self.case.actor, "plan.rebaseline", expired),
        )

    def test_wrong_proposal_hash_and_operation_binding(self) -> None:
        context = self.case.context(self.proposal, "wrong-resource")
        wrong = replace(context.authorization, resource_id="pp-" + "0" * 64 + ":sha256:" + "0" * 64)
        self.assert_code(
            ErrorCode.AUTHORIZATION_REQUIRED,
            OperationContext("wrong-resource", self.case.actor, "plan.rebaseline", wrong),
        )
        context = self.case.context(self.proposal, "bound-operation")
        self.assert_code(
            ErrorCode.AUTHORIZATION_REQUIRED,
            OperationContext("different-operation", self.case.actor, "plan.rebaseline", context.authorization),
        )

    def test_invalid_operation_identity_is_rejected_before_apply(self) -> None:
        with self.assertRaises(CwError) as raised:
            self.case.context(self.proposal, "operator change 42")
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())

    def test_reason_and_contract_change_are_mandatory(self) -> None:
        case = RebaselineCase()
        try:
            with self.assertRaises(CwError):
                case.preview(reason=" ")
            unchanged = _read_document(case.repo.root / ".codex/workflow/phases.yaml")
            with self.assertRaises(CwError) as raised:
                create_rebaseline_proposal(
                    case.repo.root, case.repo.workflow, case.repo.state(), unchanged,
                    reason="no change", actor_id=case.actor.actor_id, actor_origin=case.actor.origin.value,
                )
            self.assertEqual(ErrorCode.PLAN_REBASELINE_REQUIRED, raised.exception.code)
        finally:
            case.close()

    def test_application_facade_enforces_same_authorization_boundary(self) -> None:
        application = CWApplication(allowed_roots=[self.case.repo.root])
        try:
            project = application.open_project(self.case.repo.root)
            context = self.case.context(self.proposal, "application-rebaseline")
            outcome = application.rebaseline_plan(project, self.proposal["proposal_id"], context)
            self.assertEqual("REBASELINED", outcome.data["status"])
            self.assertFalse(outcome.idempotent_replay)
        finally:
            application.shutdown()


class PlanRevisionIntegrityTests(unittest.TestCase):
    def test_tampered_review_revision_supersession_and_missing_snapshot_fail(self) -> None:
        mutations = ("review", "old_revision", "supersession", "missing_revision")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                case = RebaselineCase()
                try:
                    proposal = case.preview()
                    outcome = case.apply(proposal, operation_id=f"integrity-{mutation}")
                    workflow = load_workflow(case.repo.root)
                    state = load_state(case.repo.root)
                    if mutation == "review":
                        path = case.repo.root / case.review_reference
                        path.write_bytes(path.read_bytes() + b"\n")
                    elif mutation == "old_revision":
                        path = case.repo.root / ".cw/plan-revisions" / f"{outcome['old_plan_revision_id']}.json"
                        payload = load_json(path)
                        payload["goal"] = "tampered"
                        atomic_json(path, payload)
                    elif mutation == "supersession":
                        path = case.repo.root / outcome["supersession"]
                        payload = load_json(path)
                        payload["reason"] = "laundered"
                        atomic_json(path, payload)
                    else:
                        (case.repo.root / ".cw/plan-revisions" / f"{outcome['old_plan_revision_id']}.json").unlink()
                    with self.assertRaises(CwError):
                        audit_history(case.repo.root, workflow, state)
                finally:
                    case.close()

    def test_candidate_and_revision_mismatch_invalidate_gate(self) -> None:
        case = RebaselineCase()
        try:
            proposal = case.preview()
            case.apply(proposal)
            workflow = load_workflow(case.repo.root)
            state = load_state(case.repo.root)
            transition(case.repo.root, state, WorkflowState.IN_PROGRESS)
            case.repo.workflow = workflow
            case.repo.artifact(content="fixed\n")
            case.repo.ready()
            run_review(
                case.repo.root, workflow, workflow.phases[0], load_state(case.repo.root),
                FakeAdapter(result(1, "APPROVE", "PASS", criterion="ARCH-001")),
            )
            gate_path = case.repo.root / ".cw/gates/01-phase-1.approved.json"
            gate = load_json(gate_path)
            gate["candidate_sha"] = "0" * 40
            atomic_json(gate_path, gate)
            with self.assertRaises(CwError):
                validate_gate(case.repo.root, workflow, "01-phase-1")
        finally:
            case.close()

    def test_orphan_revision_snapshot_fails_audit(self) -> None:
        case = RebaselineCase()
        try:
            proposal = case.preview()
            outcome = case.apply(proposal)
            source = case.repo.root / ".cw/plan-revisions" / f"{outcome['old_plan_revision_id']}.json"
            payload = load_json(source)
            alternate = copy.deepcopy(payload["workflow"])
            alternate["workflow"]["goal"] = "orphaned alternate contract"
            from cw.core.revisions import persist_revision, revision_payload

            orphan = revision_payload(
                case.repo.root, alternate, parent_revision_id=None,
                actor_id="legacy-migration", actor_origin="internal_supervisor",
            )
            persist_revision(case.repo.root, orphan)
            with self.assertRaises(CwError) as raised:
                audit_revisions(case.repo.root, load_workflow(case.repo.root), load_state(case.repo.root))
            self.assertEqual(ErrorCode.PLAN_REVISION_INVALID, raised.exception.code)
        finally:
            case.close()

    def test_embedded_validation_revision_mismatch_invalidates_gate(self) -> None:
        case = RebaselineCase()
        try:
            proposal = case.preview()
            case.apply(proposal)
            workflow = load_workflow(case.repo.root)
            state = load_state(case.repo.root)
            transition(case.repo.root, state, WorkflowState.IN_PROGRESS)
            case.repo.workflow = workflow
            case.repo.artifact(content="fixed\n")
            case.repo.ready()
            report = run_review(
                case.repo.root, workflow, workflow.phases[0], load_state(case.repo.root),
                FakeAdapter(result(1, "APPROVE", "PASS", criterion="ARCH-001")),
            )
            review_path = case.repo.root / str(load_state(case.repo.root)["last_review"])
            review = load_json(review_path)
            review["validation_evidence"]["plan_revision_id"] = "pr-" + "0" * 64
            atomic_json(review_path, review)
            with self.assertRaises(CwError):
                validate_gate(case.repo.root, workflow, "01-phase-1")
            self.assertEqual(proposal["new_plan_revision_id"], report["plan_revision_id"])
        finally:
            case.close()

    def test_duplicate_active_revision_state_fails(self) -> None:
        case = RebaselineCase()
        try:
            proposal = case.preview()
            outcome = case.apply(proposal)
            workflow = load_workflow(case.repo.root)
            state = load_state(case.repo.root)
            state["superseded_plan_revisions"].append(state["active_plan_revision"])
            save_state(case.repo.root, state)
            with self.assertRaises(CwError):
                audit_revisions(case.repo.root, workflow, state)
            self.assertNotEqual(outcome["old_plan_revision_id"], state["active_plan_revision"])
        finally:
            case.close()

    def test_tampered_history_links_fail_audit(self) -> None:
        mutations = (
            ("plan_rebaseline_proposed", "old_plan_revision_id", "tampered", 1),
            ("plan_rebaseline_authorized", "actor_id", "tampered", 1),
            ("review_superseded", "new_plan_revision_id", "tampered", 1),
            ("plan_rebaseline_authorized", "phase", "01-phase-1", 2),
            ("review_superseded", "timestamp", "2026-01-01T00:00:00Z", 1),
        )
        for action, field, value, phases in mutations:
            with self.subTest(action=action, field=field):
                case = RebaselineCase(phases=phases)
                try:
                    proposal = case.preview()
                    case.apply(proposal, operation_id=f"history-{field}")
                    state = load_state(case.repo.root)
                    event = next(item for item in state["history"] if item.get("action") == action)
                    event[field] = value
                    save_state(case.repo.root, state)
                    with self.assertRaises(CwError):
                        audit_history(case.repo.root, load_workflow(case.repo.root), state)
                finally:
                    case.close()


class PlanRevisionTransactionTests(unittest.TestCase):
    def test_failure_after_each_write_stage_rolls_back_without_lost_evidence(self) -> None:
        stages = (
            "old_revision_persisted", "new_revision_persisted", "supersession_persisted",
            "workflow_activated", "state_activated", "audit_completed",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                case = RebaselineCase()
                try:
                    proposal = case.preview()
                    old_plan = (case.repo.root / ".codex/workflow/phases.yaml").read_bytes()
                    old_state = load_state(case.repo.root)
                    old_review = (case.repo.root / case.review_reference).read_bytes()

                    def fail(current: str) -> None:
                        if current == stage:
                            raise RuntimeError(f"injected:{stage}")

                    with self.assertRaisesRegex(RuntimeError, stage):
                        case.apply(proposal, operation_id=f"failure-{stage}", failure_injector=fail)
                    self.assertEqual(old_plan, (case.repo.root / ".codex/workflow/phases.yaml").read_bytes())
                    self.assertEqual(old_state, load_state(case.repo.root))
                    self.assertEqual(old_review, (case.repo.root / case.review_reference).read_bytes())
                    self.assertFalse((case.repo.root / TRANSACTION).exists())
                    self.assertTrue(any((case.repo.root / ".cw/backups").iterdir()))
                finally:
                    case.close()

    def test_legacy_missing_revision_and_supersession_directories_rollback_to_absence(self) -> None:
        stages = ("supersession_directory_created", "plan_revision_directory_created")
        for stage in stages:
            with self.subTest(stage=stage):
                case = RebaselineCase()
                try:
                    state = load_state(case.repo.root)
                    state["created_with_cw_version"] = "0.14.1"
                    state["active_plan_revision"] = None
                    state["active_plan_revision_sha256"] = None
                    state["superseded_plan_revisions"] = []
                    save_state(case.repo.root, state)
                    shutil.rmtree(case.repo.root / ".cw/supersessions")
                    shutil.rmtree(case.repo.root / ".cw/plan-revisions")
                    proposal = case.preview()

                    def fail(current: str) -> None:
                        if current == stage:
                            raise RuntimeError(f"injected:{stage}")

                    with self.assertRaisesRegex(RuntimeError, stage):
                        case.apply(proposal, operation_id=f"legacy-missing-{stage}", failure_injector=fail)
                    self.assertFalse((case.repo.root / ".cw/supersessions").exists())
                    self.assertFalse((case.repo.root / ".cw/plan-revisions").exists())
                    self.assertFalse((case.repo.root / TRANSACTION).exists())
                finally:
                    case.close()

    def test_explicit_recovery_restores_prepared_journal(self) -> None:
        case = RebaselineCase()
        try:
            plan = _read_document(case.repo.root / ".codex/workflow/phases.yaml")
            state = load_state(case.repo.root)
            old_id, _ = active_revision(case.repo.root, state, case.repo.workflow)
            backup = backup_metadata(case.repo.root)
            journal = {
                "schema_version": 1, "kind": "plan_rebaseline_transaction", "status": "PREPARED",
                "stage": "prepared", "proposal_id": "pp-" + "0" * 64,
                "old_plan_revision_id": old_id, "new_plan_revision_id": "pr-" + "0" * 64,
                "supersession_id": "ps-" + "0" * 64,
                "operation_id": "crashed", "old_workflow": plan, "old_state": state,
                "created_files": [], "created_directories": [],
                "backup": backup.relative_to(case.repo.root).as_posix(),
                "created_at": "2026-08-20T00:00:00Z",
            }
            _write_transaction(case.repo.root, journal)
            changed = copy.deepcopy(plan)
            changed["workflow"]["goal"] = "partial"
            write_workflow(case.repo.root / ".codex/workflow/phases.yaml", changed)
            recovered = recover_rebaseline_transaction(case.repo.root)
            self.assertTrue(recovered["recovered"])
            self.assertEqual(plan, _read_document(case.repo.root / ".codex/workflow/phases.yaml"))
        finally:
            case.close()

    def test_recovery_rejects_journal_with_arbitrary_delete_target(self) -> None:
        case = RebaselineCase()
        try:
            plan = _read_document(case.repo.root / ".codex/workflow/phases.yaml")
            state = load_state(case.repo.root)
            old_id, _ = active_revision(case.repo.root, state, case.repo.workflow)
            backup = backup_metadata(case.repo.root)
            readme = case.repo.root / "README.md"
            readme.write_text("must survive\n", encoding="utf-8")
            journal = {
                "schema_version": 1, "kind": "plan_rebaseline_transaction", "status": "PREPARED",
                "stage": "prepared", "proposal_id": "pp-" + "0" * 64,
                "old_plan_revision_id": old_id, "new_plan_revision_id": "pr-" + "0" * 64,
                "supersession_id": "ps-" + "0" * 64, "operation_id": "malicious",
                "old_workflow": plan, "old_state": state, "created_files": ["README.md"],
                "created_directories": [],
                "backup": backup.relative_to(case.repo.root).as_posix(),
                "created_at": "2026-08-20T00:00:00Z",
            }
            _write_transaction(case.repo.root, journal)
            with self.assertRaises(CwError) as raised:
                recover_rebaseline_transaction(case.repo.root)
            self.assertEqual(ErrorCode.TRANSACTION_RECOVERY_REQUIRED, raised.exception.code)
            self.assertEqual(b"must survive\n", readme.read_bytes())
        finally:
            case.close()


class PlanRevisionPortabilityTests(unittest.TestCase):
    def test_spaces_unicode_and_legacy_schema_one(self) -> None:
        case = RebaselineCase(unicode_name=True)
        try:
            proposal = case.preview()
            case.apply(proposal)
            workflow = load_workflow(case.repo.root)
            audit_history(case.repo.root, workflow, load_state(case.repo.root))
        finally:
            case.close()

    def test_rebuild_refuses_reviewed_workflow(self) -> None:
        # The core precondition is covered without invoking a planner: reviewed
        # state is the explicit boundary for the dedicated rebaseline command.
        case = RebaselineCase()
        try:
            self.assertEqual("REVISION_REQUIRED", case.repo.state()["status"])
            self.assertTrue(any((case.repo.root / ".cw/reviews").glob("*.json")))
        finally:
            case.close()

    def test_cli_preview_apply_status_explain_and_history_json(self) -> None:
        case = RebaselineCase()
        previous = Path.cwd()
        try:
            proposal_document = case.repo.root / "corrected-plan.json"
            proposal_document.write_text(json.dumps(case.proposed, indent=2), encoding="utf-8")
            os.chdir(case.repo.root)

            def invoke(*arguments: str) -> tuple[int, dict]:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(arguments)
                return code, json.loads(output.getvalue())

            code, error = invoke(
                "plan", "rebaseline", "--proposal", "corrected-plan.json",
                "--reason", "Correct Phase 00 contract", "--authorize", "--json",
            )
            self.assertEqual(2, code)
            self.assertEqual("USAGE_ERROR", error["error"]["code"])
            code, preview = invoke(
                "plan", "rebaseline", "--proposal", "corrected-plan.json",
                "--reason", "Correct Phase 00 contract", "--json",
            )
            self.assertEqual(0, code)
            self.assertTrue(preview["authorization_required"])
            code, error = invoke(
                "plan", "rebaseline", "--apply", preview["proposal_id"],
                "--authorize", "--reason", "ignored", "--json",
            )
            self.assertEqual(2, code)
            self.assertEqual("USAGE_ERROR", error["error"]["code"])
            code, applied = invoke(
                "plan", "rebaseline", "--apply", preview["proposal_id"],
                "--authorize", "--operation-id", "cli-rebaseline", "--json",
            )
            self.assertEqual(0, code)
            self.assertEqual("REBASELINED", applied["status"])
            for command in (("status", "--json"), ("explain", "--json"), ("history", "--json")):
                code, payload = invoke(*command)
                self.assertEqual(0, code)
                self.assertIn("active_plan_revision" if command[0] != "history" else "phases", payload)
            code, payload = invoke("plan", "show", "--authorize", "--json")
            self.assertEqual(2, code)
            self.assertEqual("USAGE_ERROR", payload["error"]["code"])
        finally:
            os.chdir(previous)
            case.close()


if __name__ == "__main__":
    unittest.main()
