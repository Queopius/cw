from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_public_version import ROOT, _errors, _release_metadata_errors


class PublicVersionSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-version-sync-")
        self.root = Path(self.temporary.name)
        (self.root / "cw").mkdir()
        (self.root / "docs").mkdir()
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / "VERSION").write_text("0.18.0\n", encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n\n## 0.18.0 — 2026-08-28\n\n- Current.\n",
            encoding="utf-8",
        )
        self.write_history(
            [
                {"version": "0.18.0", "changes": ["Current."]},
                {"version": "0.17.0", "changes": ["Previous."]},
            ]
        )
        (self.root / "docs/release-process.md").write_text(
            'release_version="$(cat VERSION)"\n'
            'git tag -a "v${release_version}" -m "CW CLI v${release_version}"\n'
            'git push origin "v${release_version}"\n\n'
            "Core releases do not build or\nattach the public Plugin.\n",
            encoding="utf-8",
        )
        (self.root / ".github/workflows/release-check.yml").write_text(
            "python scripts/build_release.py --output dist --channel stable --component core\n"
            "python scripts/validate_release_assets.py --directory dist --component core\n"
            "assets=(\n"
            '  "dist/codex_workflow-${release_version}-py3-none-any.whl"\n'
            '  "dist/codex_workflow-${release_version}.tar.gz"\n'
            '  "dist/cw-${release_version}-linux-x86_64.tar.gz"\n'
            '  "dist/cw-release-manifest.json"\n'
            ")\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_history(self, releases: object, schema: object = 1) -> None:
        (self.root / "cw/release_history.json").write_text(
            json.dumps({"schema_version": schema, "releases": releases}), encoding="utf-8"
        )

    def issues(self) -> list[str]:
        return _release_metadata_errors(self.root)

    def test_valid_metadata_and_real_snapshot_pass(self) -> None:
        self.assertEqual([], self.issues())
        self.assertEqual([], _errors(ROOT))

    def test_current_version_absent_or_under_unreleased_is_rejected(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## Unreleased\n\n- 0.18.0 notes.\n", encoding="utf-8")
        self.assertTrue(self.issues())
        changelog.write_text(
            "# Changelog\n\n## Unreleased\n\n- Pending.\n\n## 0.18.0 — 2026-08-28\n",
            encoding="utf-8",
        )
        self.assertIn("The current dated release heading", " ".join(self.issues()))

    def test_duplicate_or_undated_or_malformed_changelog_heading_is_rejected(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        for text in (
            "## Unreleased\n\n## 0.18.0 — 2026-08-28\n## 0.18.0 — 2026-08-28\n",
            "## Unreleased\n\n## 0.18.0\n",
            "## Unreleased\n\n## 0.18.0 — 2026-99-99\n",
        ):
            with self.subTest(text=text):
                changelog.write_text(text, encoding="utf-8")
                self.assertTrue(self.issues())

    def test_corrupt_schema_empty_and_wrong_first_history_are_rejected(self) -> None:
        history = self.root / "cw/release_history.json"
        history.write_text("{", encoding="utf-8")
        self.assertTrue(self.issues())
        self.write_history([], schema=2)
        self.assertTrue(self.issues())
        self.write_history([{"version": "0.17.0", "changes": ["Old."]}])
        self.assertIn("first release history", " ".join(self.issues()))

    def test_duplicate_invalid_ascending_and_invalid_changes_are_rejected(self) -> None:
        cases = (
            [
                {"version": "0.18.0", "changes": ["A"]},
                {"version": "0.18.0", "changes": ["B"]},
            ],
            [{"version": "v0.18.0", "changes": ["A"]}],
            [
                {"version": "0.18.0", "changes": ["A"]},
                {"version": "0.19.0", "changes": ["B"]},
            ],
            [{"version": "0.18.0", "changes": []}],
            [{"version": "0.18.0", "changes": [7]}],
        )
        for releases in cases:
            with self.subTest(releases=releases):
                self.write_history(releases)
                self.assertTrue(self.issues())

    def test_version_must_be_semver(self) -> None:
        (self.root / "VERSION").write_text("v0.18.0\n", encoding="utf-8")
        self.assertIn("not valid SemVer", " ".join(self.issues()))

    def test_tag_documentation_must_use_version_without_historical_literal(self) -> None:
        process = self.root / "docs/release-process.md"
        process.write_text("git tag -a v0.17.0 -m old\n", encoding="utf-8")
        issues = " ".join(self.issues())
        self.assertIn("derive tag identity", issues)
        self.assertIn("hardcode", issues)

    def test_workflow_must_keep_exact_core_only_asset_allowlist(self) -> None:
        workflow = self.root / ".github/workflows/release-check.yml"
        original = workflow.read_text(encoding="utf-8")
        workflow.write_text(
            original.replace(")\n", '  "dist/cw-plugin-0.1.0.zip"\n)\n'), encoding="utf-8"
        )
        issues = " ".join(self.issues())
        self.assertIn("four Core assets", issues)
        self.assertIn("Core-only profile", issues)


if __name__ == "__main__":
    unittest.main()
