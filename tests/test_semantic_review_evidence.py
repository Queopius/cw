from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError, replace

from cw.adapters.result import CodexResult
from cw.agents.reviewer import reviewer_prompt, run_review
from cw.checks.deterministic import load_readiness, validate_phase
from cw.checks.review_evidence import (
    MAX_ARTIFACT_BYTES,
    SemanticReviewEvidenceBundle,
    build_semantic_review_evidence_bundle,
)
from cw.checks.verification import validate_verification_receipt
from cw.core.errors import CwError, ErrorCode
from cw.core.models import (
    CompletionContract,
    CompletionRequirement,
    RequiredCommand,
)
from cw.core.utils import sha256_bytes, sha256_file
from tests.helpers import FakeAdapter, TempRepo, result


class CapturingAdapter:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or result()
        self.prompt: str | None = None
        self.calls = 0

    def run_reviewer(self, root, prompt, schema, timeout):
        self.calls += 1
        self.prompt = prompt
        return CodexResult(self.payload, "")


class SemanticReviewEvidenceBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.repo.artifact(content="first\r\nsecond\rthird\n")
        self.repo.ready()

    def tearDown(self) -> None:
        self.repo.close()

    def validated_materials(self):
        phase = self.repo.workflow.phases[0]
        validation = validate_phase(self.repo.root, self.repo.workflow, phase)
        self.assertTrue(validation.passed, validation.errors)
        self.assertIsNotNone(validation.receipt)
        reference = validation.receipt
        assert reference is not None
        receipt = validate_verification_receipt(
            self.repo.root,
            self.repo.workflow,
            phase,
            reference["reference"],
            reference["sha256"],
        )
        return phase, load_readiness(self.repo.root, phase), reference, receipt

    def bundle(self) -> SemanticReviewEvidenceBundle:
        phase, readiness, reference, receipt = self.validated_materials()
        return build_semantic_review_evidence_bundle(
            self.repo.root,
            self.repo.workflow,
            phase,
            readiness,
            reference,
            receipt,
        )

    def test_bundle_is_immutable_and_built_from_valid_readiness_and_receipt(self) -> None:
        bundle = self.bundle()
        payload = bundle.to_dict()

        self.assertEqual("cw.semantic-review-evidence.v1", payload["schema"])
        self.assertEqual("READY_FOR_REVIEW", payload["readiness"]["status"])
        self.assertEqual("PASSED", payload["deterministic_verification"]["result"])
        self.assertEqual(
            payload["verification_receipt"]["receipt_id"],
            payload["readiness"]["verification_receipt"]["receipt_id"],
        )
        self.assertEqual(
            sha256_bytes(bundle.canonical_json.encode("utf-8")), bundle.sha256
        )
        with self.assertRaises(FrozenInstanceError):
            bundle.sha256 = "sha256:" + "0" * 64  # type: ignore[misc]

    def test_declared_artifact_content_is_normalized_and_hash_verified(self) -> None:
        bundle = self.bundle().to_dict()
        artifact = bundle["artifacts"][0]

        self.assertEqual("docs/phase-1.md", artifact["path"])
        self.assertEqual("first\nsecond\nthird\n", artifact["content"])
        self.assertEqual(
            sha256_file(self.repo.root / "docs/phase-1.md"), artifact["sha256"]
        )
        self.assertEqual("line-endings-to-lf", artifact["text_normalization"])

    def test_undeclared_file_is_excluded(self) -> None:
        private = self.repo.root / "docs/private-note.md"
        private.write_text("PRIVATE-CONTENT-MUST-NOT-LEAK", encoding="utf-8")

        serialized = self.bundle().canonical_json

        self.assertNotIn("private-note.md", serialized)
        self.assertNotIn("PRIVATE-CONTENT-MUST-NOT-LEAK", serialized)

    def test_traversal_is_rejected(self) -> None:
        _, readiness, reference, receipt = self.validated_materials()
        phase = replace(self.repo.workflow.phases[0], artifacts=("../private.md",))
        workflow = replace(
            self.repo.workflow, phases=(phase, *self.repo.workflow.phases[1:])
        )
        readiness = copy.deepcopy(readiness)
        receipt = copy.deepcopy(receipt)
        readiness["artifacts"] = ["../private.md"]
        receipt["artifact_identities"] = {
            "../private.md": "sha256:" + "0" * 64
        }

        with self.assertRaises(CwError) as raised:
            build_semantic_review_evidence_bundle(
                self.repo.root, workflow, phase, readiness, reference, receipt
            )

        self.assertEqual(ErrorCode.REVIEW_EVIDENCE_UNAVAILABLE, raised.exception.code)
        self.assertIn("unsafe_relative_path", raised.exception.details or "")

    def test_external_symlink_is_rejected(self) -> None:
        _, readiness, reference, receipt = self.validated_materials()
        outside = self.repo.root.parent / "outside-private.md"
        outside.write_text("outside", encoding="utf-8")
        link = self.repo.root / "docs/external.md"
        link.symlink_to(outside)
        phase = replace(self.repo.workflow.phases[0], artifacts=("docs/external.md",))
        workflow = replace(
            self.repo.workflow, phases=(phase, *self.repo.workflow.phases[1:])
        )
        readiness = copy.deepcopy(readiness)
        receipt = copy.deepcopy(receipt)
        readiness["artifacts"] = ["docs/external.md"]
        receipt["artifact_identities"] = {
            "docs/external.md": sha256_file(outside)
        }

        with self.assertRaises(CwError) as raised:
            build_semantic_review_evidence_bundle(
                self.repo.root, workflow, phase, readiness, reference, receipt
            )

        self.assertEqual(ErrorCode.REVIEW_EVIDENCE_UNAVAILABLE, raised.exception.code)
        self.assertIn("missing_or_outside_project", raised.exception.details or "")

    def test_hash_mismatch_is_rejected(self) -> None:
        phase, readiness, reference, receipt = self.validated_materials()
        receipt = copy.deepcopy(receipt)
        receipt["artifact_identities"][phase.artifacts[0]] = "sha256:" + "0" * 64

        with self.assertRaises(CwError) as raised:
            build_semantic_review_evidence_bundle(
                self.repo.root,
                self.repo.workflow,
                phase,
                readiness,
                reference,
                receipt,
            )

        self.assertEqual(ErrorCode.REVIEW_EVIDENCE_UNAVAILABLE, raised.exception.code)
        self.assertIn("hash_mismatch", raised.exception.details or "")

    def test_oversized_artifact_fails_closed_before_reviewer(self) -> None:
        self.repo.artifact(content="x" * (MAX_ARTIFACT_BYTES + 1))
        adapter = CapturingAdapter()

        with self.assertRaises(CwError) as raised:
            run_review(
                self.repo.root,
                self.repo.workflow,
                self.repo.workflow.phases[0],
                self.repo.state(),
                adapter,
            )

        self.assertEqual(ErrorCode.REVIEW_EVIDENCE_UNAVAILABLE, raised.exception.code)
        self.assertEqual(0, adapter.calls)
        self.assertEqual(0, self.repo.state()["attempt"])

    def test_prompt_contains_criteria_artifacts_commands_and_completion_contract(self) -> None:
        phase, readiness, reference, receipt = self.validated_materials()
        command = RequiredCommand("python -c pass", 30)
        phase = replace(
            phase,
            required_commands=(command,),
            completion_requirements=("DELIVERED",),
        )
        workflow = replace(
            self.repo.workflow,
            phases=(phase, *self.repo.workflow.phases[1:]),
            completion_target=CompletionContract(
                id="complete",
                name="Complete",
                description="All work is delivered",
                target_type="project",
                requirements=(
                    CompletionRequirement(
                        id="DELIVERED",
                        description="The phase is delivered",
                        evidence_expectations=("declared artifact",),
                    ),
                ),
            ),
        )
        readiness = copy.deepcopy(readiness)
        receipt = copy.deepcopy(receipt)
        readiness["checks_executed"] = [
            {"command": command.command, "exit_code": 0}
        ]
        receipt["commands"] = [
            {
                "index": 0,
                "command": command.command,
                "argv": ["python", "-c", "pass"],
                "cwd": ".",
                "timeout_seconds": 30,
                "exit_code": 0,
                "duration_ms": 1,
                "stdout_sha256": sha256_bytes(b""),
                "stderr_sha256": sha256_bytes(b""),
            }
        ]
        bundle = build_semantic_review_evidence_bundle(
            self.repo.root, workflow, phase, readiness, reference, receipt
        )

        prompt = reviewer_prompt(bundle)

        self.assertIn("P1-001", prompt)
        self.assertIn("docs/phase-1.md", prompt)
        self.assertIn('"exit_code":0', prompt)
        self.assertIn("DELIVERED", prompt)
        self.assertIn("NEVER calculate or recalculate hashes", prompt)
        self.assertIn("NEVER explore or read the filesystem", prompt)

    def test_complete_bundle_allows_normal_semantic_flow_without_commands(self) -> None:
        adapter = CapturingAdapter()

        report = run_review(
            self.repo.root,
            self.repo.workflow,
            self.repo.workflow.phases[0],
            self.repo.state(),
            adapter,
        )

        self.assertEqual("APPROVE", report["decision"])
        self.assertEqual(1, report["attempt"])
        self.assertEqual(1, adapter.calls)
        self.assertIn("first\\nsecond\\nthird\\n", adapter.prompt or "")

    def test_private_artifact_content_is_not_in_public_failure_evidence(self) -> None:
        marker = "PRIVATE-ARTIFACT-CONTENT-93A7"
        self.repo.artifact(content=marker)
        error = CwError(
            "provider included private evidence",
            ErrorCode.REVIEWER_PROCESS_ERROR,
            details=f"provider diagnostic echoed {marker}",
        )

        with self.assertRaises(CwError) as raised:
            run_review(
                self.repo.root,
                self.repo.workflow,
                self.repo.workflow.phases[0],
                self.repo.state(),
                FakeAdapter(error=error),
            )

        state = self.repo.state()
        report = (self.repo.root / state["last_review"]).read_text(encoding="utf-8")
        self.assertNotIn(marker, raised.exception.message)
        self.assertNotIn(marker, raised.exception.details or "")
        self.assertNotIn(marker, state["last_error"])
        self.assertNotIn(marker, report)
        self.assertEqual(0, state["attempt"])


if __name__ == "__main__":
    unittest.main()
