from __future__ import annotations

import json
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cw.adapters.result import CodexRunResult
from cw.agents.reviewer import run_review
from cw.cli.commands.execution import command_retry
from cw.core.authorization import Actor, ActorOrigin, issue_user_authorization
from cw.core.completion import (
    authorize_extension,
    completion_gate_path,
    run_completion_review,
    validate_completion_gate,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.initialize import repair
from cw.core.audit import audit_history
from cw.core.revisions import persist_revision, revision_payload
from cw.core.progress import derive_effective_workflow_state
from cw.core.state import load_state, save_state
from cw.core.workflow import _read_document, load_workflow, write_workflow, workflow_hash
from cw.planning.planner import Planner
from cw.ui.console import Console
from tests.helpers import FakeAdapter, TempRepo, result


class CompletionBackend:
    def __init__(self, review: dict | None = None, phases: list[dict] | None = None, error: CwError | None = None):
        self.review = review
        self.phases = phases
        self.error = error
        self.review_calls = 0
        self.planner_calls = 0

    def run_completion_reviewer(self, root, prompt, schema, timeout):
        self.review_calls += 1
        if self.error:
            raise self.error
        return CodexRunResult(self.review, "")

    def run_extension_planner(self, root, prompt, schema, timeout):
        self.planner_calls += 1
        return CodexRunResult({"phases": self.phases}, "")


class CompletionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo(phases=2)

    def tearDown(self) -> None:
        self.repo.close()

    def adopt(self, target: str = "functional-prototype") -> None:
        path = self.repo.root / ".codex/workflow/phases.yaml"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["completion_target"] = Planner.completion_contract("Deliver a tested sample application", target_type=target)
        write_workflow(path, document)
        self.repo.workflow = load_workflow(self.repo.root)
        state = self.repo.state()
        state["workflow_sha256"] = workflow_hash(path)
        save_state(self.repo.root, state)

    def approve_phases(self, through: int = 2) -> None:
        for phase in range(1, through + 1):
            self.repo.artifact(phase)
            self.repo.ready(phase)
            run_review(
                self.repo.root, self.repo.workflow, self.repo.workflow.phases[phase - 1],
                self.repo.state(), FakeAdapter(result(phase)),
            )

    def completion_result(self, decision: str = "SATISFIED", missing: str | None = None) -> dict:
        contract = self.repo.workflow.completion_target
        assert contract is not None
        evidence = "docs/phase-2.md:1 completion evidence"
        results = []
        for requirement in contract.requirements:
            is_missing = requirement.id == missing
            results.append({
                "id": requirement.id,
                "status": "MISSING" if is_missing else "VERIFIED",
                "evidence": ["MISSING: required system evidence"] if is_missing else [evidence],
                "rationale": "Evidence is absent" if is_missing else "Verified in the final repository",
            })
        return {
            "decision": decision,
            "contract_results": results,
            "system_findings": [] if missing is None else [{
                "category": "system-composition", "severity": "blocking",
                "summary": "Required cross-component evidence is absent",
                "evidence": ["MISSING: required system evidence"],
                "requirement_ids": [missing],
            }],
            "missing_evidence": [] if missing is None else ["Required system evidence"],
            "extension_recommendation": {
                "rationale": "" if missing is None else "Add one coherent hardening phase",
                "requirement_ids": [] if missing is None else [missing],
            },
            "summary": "Completion target evaluated",
        }

    def extension_phase(self, requirement: str, number: int = 3) -> dict:
        previous = self.repo.workflow.phases[-1].id
        return {
            "id": f"{number:02d}-system-hardening", "name": "System Hardening",
            "objective": "Close the verified system-level completion gap.",
            "depends_on": [previous], "artifacts": [f"docs/phase-{number}.md"],
            "review_paths": ["docs/**/*"], "required_commands": [],
            "acceptance_criteria": [{
                "id": f"EXT-{number:02d}-001", "severity": "blocking",
                "description": "The system-level gap is closed with evidence.",
            }],
            "blocking_criteria": ["Required completion evidence remains absent"],
            "requires_human_approval": False,
            "expected_evidence": ["Focused system-level verification"],
            "completion_requirements": [requirement],
        }

    def authorization(self, action: str = "extension.approve"):
        reference = self.repo.state().get("extension_proposal")
        assert isinstance(reference, str)
        return issue_user_authorization(
            action=action,
            resource_id=reference,
            operation_id=f"test-operation-{action}-{self.repo.state().get('completion_cycle', 0)}",
            actor=Actor("test-operator", ActorOrigin.HUMAN_CLI, explicit_user_intent=True),
        )

    def test_legacy_completion_semantics_are_preserved(self) -> None:
        self.approve_phases()
        state = self.repo.state()
        effective = derive_effective_workflow_state(self.repo.root, self.repo.workflow, state)
        self.assertEqual("COMPLETED", state["status"])
        self.assertEqual("legacy", effective.completion_mode)
        self.assertTrue(effective.is_complete)
        self.assertFalse(completion_gate_path(self.repo.root).exists())

    def test_contract_all_gates_becomes_planned_complete_not_semantically_complete(self) -> None:
        self.adopt()
        self.approve_phases()
        state = self.repo.state()
        effective = derive_effective_workflow_state(self.repo.root, self.repo.workflow, state)
        self.assertEqual("PLANNED_COMPLETE", state["status"])
        self.assertIsNone(state["current_phase"])
        self.assertTrue(effective.planned_scope_complete)
        self.assertFalse(effective.is_complete)

    def test_satisfied_review_creates_distinct_completion_evidence(self) -> None:
        self.adopt()
        self.approve_phases()
        backend = CompletionBackend(self.completion_result())
        report = run_completion_review(self.repo.root, self.repo.workflow, self.repo.state(), backend)
        self.assertEqual("SATISFIED", report["decision"])
        self.assertEqual("COMPLETED", self.repo.state()["status"])
        self.assertTrue(completion_gate_path(self.repo.root).is_file())
        validate_completion_gate(self.repo.root, self.repo.workflow)
        self.assertEqual(1, backend.review_calls)
        self.assertEqual(0, backend.planner_calls)

    def test_extension_requires_human_authorization_and_preserves_old_gates(self) -> None:
        self.adopt()
        self.approve_phases()
        old_gates = {path.name: path.read_bytes() for path in (self.repo.root / ".cw/gates").glob("*.json")}
        missing = self.repo.workflow.completion_target.requirements[0].id
        backend = CompletionBackend(
            self.completion_result("EXTENSION_REQUIRED", missing),
            [self.extension_phase(missing)],
        )
        report = run_completion_review(self.repo.root, self.repo.workflow, self.repo.state(), backend)
        self.assertEqual("EXTENSION_REQUIRED", report["decision"])
        self.assertEqual("EXTENSION_PROPOSED", self.repo.state()["status"])
        self.assertIsNone(self.repo.state()["current_phase"])
        self.assertEqual(2, len(load_workflow(self.repo.root).phases))
        self.assertFalse(completion_gate_path(self.repo.root).exists())

        authorization = authorize_extension(
            self.repo.root, self.repo.workflow, self.repo.state(), approve=True,
            authorization=self.authorization(),
        )
        extended = load_workflow(self.repo.root)
        self.assertEqual("03-system-hardening", authorization["current_phase"])
        self.assertEqual(3, len(extended.phases))
        self.assertEqual("IN_PROGRESS", self.repo.state()["status"])
        self.assertEqual(old_gates, {path.name: path.read_bytes() for path in (self.repo.root / ".cw/gates").glob("*.json")})

    def test_rejected_extension_does_not_mutate_phases(self) -> None:
        self.adopt()
        self.approve_phases()
        missing = self.repo.workflow.completion_target.requirements[0].id
        run_completion_review(
            self.repo.root, self.repo.workflow, self.repo.state(),
            CompletionBackend(self.completion_result("EXTENSION_REQUIRED", missing), [self.extension_phase(missing)]),
        )
        before = (self.repo.root / ".codex/workflow/phases.yaml").read_bytes()
        authorize_extension(
            self.repo.root, self.repo.workflow, self.repo.state(), approve=False,
            authorization=self.authorization("extension.reject"),
        )
        self.assertEqual(before, (self.repo.root / ".codex/workflow/phases.yaml").read_bytes())
        self.assertEqual("PLANNED_COMPLETE", self.repo.state()["status"])

    def test_extension_phase_uses_normal_gate_flow_then_reviews_again(self) -> None:
        self.adopt()
        self.approve_phases()
        missing = self.repo.workflow.completion_target.requirements[0].id
        run_completion_review(
            self.repo.root, self.repo.workflow, self.repo.state(),
            CompletionBackend(self.completion_result("EXTENSION_REQUIRED", missing), [self.extension_phase(missing)]),
        )
        authorize_extension(
            self.repo.root, self.repo.workflow, self.repo.state(), approve=True,
            authorization=self.authorization(),
        )
        self.repo.workflow = load_workflow(self.repo.root)
        self.repo.artifact(3)
        self.repo.ready(3)
        readiness_path = self.repo.root / ".cw/runtime/READY_FOR_REVIEW.json"
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        readiness["phase"] = "03-system-hardening"
        readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
        extension_review = {
            "decision": "APPROVE", "summary": "extension approved", "blocking_issues": [],
            "criteria": [{
                "id": "EXT-03-001", "status": "PASS",
                "evidence": ["docs/phase-3.md:1 extension evidence"],
            }],
            "blocking_criteria": [{
                "description": "Required completion evidence remains absent", "status": "PASS",
                "evidence": ["docs/phase-3.md:1 extension evidence"],
            }],
        }
        run_review(
            self.repo.root, self.repo.workflow, self.repo.workflow.phases[2],
            self.repo.state(), FakeAdapter(extension_review),
        )
        self.assertEqual("PLANNED_COMPLETE", self.repo.state()["status"])
        report = run_completion_review(
            self.repo.root, self.repo.workflow, self.repo.state(),
            CompletionBackend(self.completion_result()),
        )
        self.assertEqual("SATISFIED", report["decision"])
        self.assertEqual(2, self.repo.state()["completion_cycle"])

    def test_completion_reviewer_infrastructure_failure_is_retryable_blocked(self) -> None:
        self.adopt()
        self.approve_phases()
        backend = CompletionBackend(error=CwError("offline", ErrorCode.REVIEWER_NETWORK_ERROR))
        with self.assertRaises(CwError):
            run_completion_review(self.repo.root, self.repo.workflow, self.repo.state(), backend)
        state = self.repo.state()
        self.assertEqual("COMPLETION_BLOCKED", state["status"])
        self.assertEqual("completion_review", state["infrastructure_error"]["operation"])
        self.assertFalse(completion_gate_path(self.repo.root).exists())

        calls: list[str] = []
        args = SimpleNamespace(json=True)
        code = command_retry(
            args,
            Console(stream=io.StringIO()),
            root_resolver=lambda: self.repo.root,
            context=lambda _root: (None, self.repo.state(), self.repo.workflow),
            current_resolver=lambda workflow, state: workflow.phase(state["current_phase"]),
            review_command=lambda *_args: self.fail("phase review retry was selected"),
            start_command=lambda *_args: self.fail("implementation retry was selected"),
            plan_command=lambda *_args: self.fail("planning retry was selected"),
            completion_command=lambda retry_args, _console: calls.append(retry_args.action) or 0,
        )
        self.assertEqual(0, code)
        self.assertEqual(["review"], calls)

    def test_malformed_completion_schema_fails_closed(self) -> None:
        self.adopt()
        self.approve_phases()
        with self.assertRaises(CwError):
            run_completion_review(
                self.repo.root, self.repo.workflow, self.repo.state(),
                CompletionBackend({"decision": "SATISFIED"}),
            )
        self.assertEqual("COMPLETION_BLOCKED", self.repo.state()["status"])

    def test_repair_recovers_stale_completed_metadata_to_extension_proposed(self) -> None:
        self.adopt()
        self.approve_phases()
        missing = self.repo.workflow.completion_target.requirements[0].id
        run_completion_review(
            self.repo.root, self.repo.workflow, self.repo.state(),
            CompletionBackend(self.completion_result("EXTENSION_REQUIRED", missing), [self.extension_phase(missing)]),
        )
        state = self.repo.state()
        state["status"] = "COMPLETED"
        state["extension_proposal"] = None
        save_state(self.repo.root, state)
        repair(self.repo.root)
        repaired = load_state(self.repo.root)
        self.assertEqual("EXTENSION_PROPOSED", repaired["status"])
        self.assertIsNotNone(repaired["extension_proposal"])

    def test_repair_recovers_valid_completion_evidence_from_stale_state(self) -> None:
        self.adopt()
        self.approve_phases()
        run_completion_review(
            self.repo.root, self.repo.workflow, self.repo.state(),
            CompletionBackend(self.completion_result()),
        )
        state = self.repo.state()
        state["status"] = "PLANNED_COMPLETE"
        state["last_completion_gate"] = None
        save_state(self.repo.root, state)
        repair(self.repo.root)
        repaired = self.repo.state()
        self.assertEqual("COMPLETED", repaired["status"])
        self.assertEqual(".cw/completion/completion.satisfied.json", repaired["last_completion_gate"])

    def test_repair_finishes_durably_authorized_extension_after_interruption(self) -> None:
        self.adopt()
        self.approve_phases()
        document = _read_document(self.repo.root / ".codex/workflow/phases.yaml")
        revision = revision_payload(
            self.repo.root, document, parent_revision_id=None,
            actor_id="local-operator", actor_origin="human_cli",
        )
        persist_revision(self.repo.root, revision)
        state = self.repo.state()
        state["active_plan_revision"] = revision["plan_revision_id"]
        state["active_plan_revision_sha256"] = revision["canonical_workflow_sha256"]
        save_state(self.repo.root, state)
        missing = self.repo.workflow.completion_target.requirements[0].id
        run_completion_review(
            self.repo.root, self.repo.workflow, self.repo.state(),
            CompletionBackend(self.completion_result("EXTENSION_REQUIRED", missing), [self.extension_phase(missing)]),
        )
        with patch("cw.core.revisions.persist_revision", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                authorize_extension(
                    self.repo.root, self.repo.workflow, self.repo.state(), approve=True,
                    authorization=self.authorization(),
                )
        self.assertEqual(3, len(load_workflow(self.repo.root).phases))
        repair(self.repo.root)
        recovered_workflow = load_workflow(self.repo.root)
        recovered_state = self.repo.state()
        self.assertEqual(3, len(recovered_workflow.phases))
        self.assertEqual("03-system-hardening", recovered_state["current_phase"])
        self.assertNotEqual(revision["plan_revision_id"], recovered_state["active_plan_revision"])
        self.assertIn(revision["plan_revision_id"], recovered_state["superseded_plan_revisions"])
        audit_history(self.repo.root, recovered_workflow, recovered_state)


class PlannerCompletionIntentTests(unittest.TestCase):
    def test_poc_does_not_inherit_production_contract(self) -> None:
        contract = Planner.completion_contract("Build a proof of concept for local search")
        ids = {item["id"] for item in contract["requirements"]}
        self.assertEqual("proof-of-concept", contract["target_type"])
        self.assertNotIn("OPERATIONS_READY", ids)
        self.assertNotIn("CHANGE_SAFETY", ids)

    def test_controlled_pilot_includes_safety_and_acceptance(self) -> None:
        contract = Planner.completion_contract("Ready for a first controlled customer pilot")
        ids = {item["id"] for item in contract["requirements"]}
        self.assertEqual("controlled-pilot", contract["target_type"])
        self.assertTrue({"FAILURE_SAFETY", "TARGET_ACCEPTANCE", "SECURITY_BASELINE"}.issubset(ids))

    def test_production_contract_is_stronger(self) -> None:
        contract = Planner.completion_contract("Make the CLI production-ready")
        ids = {item["id"] for item in contract["requirements"]}
        self.assertTrue({"OPERATIONS_READY", "CHANGE_SAFETY", "INSTALL_RUNTIME"}.issubset(ids))


if __name__ == "__main__":
    unittest.main()
