from __future__ import annotations

import copy
import json
import shutil
import unittest
from pathlib import Path

from cw.core.errors import CwError, ErrorCode
from cw.core.rebaseline_recovery import (
    TRANSACTION,
    _directory_digest,
    _recovery_backup_reference,
    _recovery_id,
    _recovery_reference,
    _validate_recovery_receipts,
    apply_rebaseline_recovery,
)
from cw.core.utils import atomic_json
from tests.test_rebaseline_recovery import RecoveryCase


class RebaselineSecurityCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = RecoveryCase()

    def tearDown(self) -> None:
        self.case.close()

    def test_joint_gate_backup_receipt_tamper_rejects_with_external_authority(self) -> None:
        _receipt_path, receipt, _state_path, _state = self.case.evidence()
        gate = self.case.repo.root / self.case.gate_reference
        backup_gate = self.case.repo.root / receipt["backup"] / "gates" / Path(self.case.gate_reference).name
        for path in (gate, backup_gate):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["synthetic_tamper"] = "coherent-local-substitution"
            path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        receipt["backup_sha256"] = _directory_digest(
            self.case.repo.root / receipt["backup"], "Recovery backup",
        )
        atomic_json(_receipt_path, receipt)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_structural_receipt_validation_never_authorizes_replay(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        substituted = copy.deepcopy(receipt)
        substituted["request"]["reason"] = "coherent persisted substitution"
        atomic_json(receipt_path, substituted)
        # This path intentionally performs only structural validation. It must
        # not return an idempotent result or claim external authorization.
        with self.assertRaises(CwError):
            _validate_recovery_receipts(self.case.repo.root)
        with self.assertRaises(CwError):
            self.case.apply()

    def test_coherent_persisted_request_identity_substitution_is_not_replay(self) -> None:
        receipt_path, receipt, _state_path, _state = self.case.evidence()
        substituted = copy.deepcopy(receipt)
        substituted["request"]["reason"] = "coherent persisted substitution"
        new_id = _recovery_id(substituted["request"])
        old_backup = self.case.repo.root / substituted["backup"]
        new_backup_reference = _recovery_backup_reference(new_id)
        new_backup = self.case.repo.root / new_backup_reference
        shutil.move(old_backup, new_backup)
        substituted["recovery_id"] = new_id
        substituted["operation_id"] = new_id
        substituted["correlation_id"] = new_id
        substituted["backup"] = new_backup_reference
        substituted["provenance"]["backup"] = new_backup_reference
        substituted["backup_sha256"] = _directory_digest(new_backup, "Recovery backup")
        new_receipt = self.case.repo.root / _recovery_reference(new_id)
        receipt_path.unlink()
        atomic_json(new_receipt, substituted)
        _validate_recovery_receipts(self.case.repo.root)
        with self.assertRaises(CwError) as raised:
            self.case.apply()
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, raised.exception.code)

    def test_expected_gate_reference_and_digest_are_required(self) -> None:
        with self.assertRaises(CwError) as missing:
            apply_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha,
                "missing external gate authority",
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, missing.exception.code)
        with self.assertRaises(CwError) as wrong_ref:
            apply_rebaseline_recovery(
                self.case.repo.root, "02-phase-2", self.case.review_reference,
                self.case.review_sha, self.case.workflow_sha, self.case.state_sha,
                "wrong gate reference", expected_prior_gate_reference=".cw/gates/nope.json",
                expected_prior_gate_sha256=self.case.gate_sha,
            )
        self.assertEqual(ErrorCode.OPERATION_CONFLICT, wrong_ref.exception.code)

    def test_failure_after_prepared_leaves_no_backup_or_transaction(self) -> None:
        def fail(step: str) -> None:
            if step == "journal_persisted":
                raise RuntimeError("after PREPARED")

        with self.assertRaisesRegex(RuntimeError, "after PREPARED"):
            self.case.apply(failure_injector=fail)
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())
        self.assertEqual([], list((self.case.repo.root / ".cw/backups").glob("rebaseline-recovery-*")))

    def test_failure_during_backup_is_recoverable_and_no_orphan_remains(self) -> None:
        def fail(step: str) -> None:
            if step == "backup_created":
                raise RuntimeError("during backup")

        with self.assertRaisesRegex(RuntimeError, "during backup"):
            self.case.apply(failure_injector=fail)
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())
        self.assertEqual([], list((self.case.repo.root / ".cw/backups").glob("rebaseline-recovery-*")))

    def test_failure_after_backup_before_ready_is_recoverable(self) -> None:
        def fail(step: str) -> None:
            if step == "backup_ready":
                raise RuntimeError("after backup")

        with self.assertRaisesRegex(RuntimeError, "after backup"):
            self.case.apply(failure_injector=fail)
        self.assertFalse((self.case.repo.root / TRANSACTION).exists())
        self.assertEqual([], list((self.case.repo.root / ".cw/backups").glob("rebaseline-recovery-*")))

    def test_exact_external_replay_remains_idempotent(self) -> None:
        first = self.case.apply()
        second = self.case.apply()
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["operation_id"], second["operation_id"])


if __name__ == "__main__":
    unittest.main()
