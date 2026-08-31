from __future__ import annotations

import unittest

from cw.core.reviews import normalize_evidence_references


class RevisionCompatibilityTests(unittest.TestCase):
    def test_revisions_lane_imports_the_normalizer_used_by_recovery_regressions(self) -> None:
        normalized, references = normalize_evidence_references(
            ["tests/test_revisions.py:1 recovery regression"],
            evidence_paths=frozenset({"tests/test_revisions.py"}),
        )
        self.assertEqual(
            ["tests/test_revisions.py:1 recovery regression"], normalized,
        )
        self.assertEqual(
            [("tests/test_revisions.py:1", "tests/test_revisions.py")], references,
        )


if __name__ == "__main__":
    unittest.main()
