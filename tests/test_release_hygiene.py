from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseHygieneTests(unittest.TestCase):
    def test_installed_reviewer_schema_matches_runtime_schema(self) -> None:
        self.assertEqual(
            json.loads((ROOT / "cw/schemas/phase-review.schema.json").read_text(encoding="utf-8")),
            json.loads((ROOT / "cw/templates/.codex/schemas/phase-review.schema.json").read_text(encoding="utf-8")),
        )

    def test_ci_covers_supported_python_versions(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', workflow)
        self.assertIn("python -m build", workflow)
        self.assertIn("cw version --json", workflow)
        self.assertIn("cw --version", workflow)
        self.assertIn("python scripts/build_release.py --output dist --channel stable", workflow)
        self.assertIn('cw-plugin-$(cat plugins/cw/VERSION).zip', workflow)

    def test_actions_are_pinned_to_full_commit_shas(self) -> None:
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            references = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", text)
            self.assertTrue(references, workflow.name)
            for reference in references:
                self.assertRegex(reference, r"^[0-9a-f]{40}$", workflow.name)

    def test_release_tags_must_come_from_release_and_match_version(self) -> None:
        workflow = (ROOT / ".github/workflows/release-check.yml").read_text(encoding="utf-8")
        self.assertIn('git fetch origin release', workflow)
        self.assertIn('origin/release', workflow)
        self.assertIn('GITHUB_REF_NAME#v', workflow)
        self.assertIn('VERSION', workflow)
        self.assertIn("python scripts/build_release.py --output dist --channel stable", workflow)
        self.assertIn('cw-plugin-$(cat plugins/cw/VERSION).zip', workflow)
        self.assertNotIn('cw-plugin-$(cat VERSION).zip', workflow)

    def test_native_managed_installations_verify_both_version_surfaces(self) -> None:
        workflow = (ROOT / ".github/workflows/cross-platform.yml").read_text(encoding="utf-8")
        self.assertIn('"$CW_BIN_DIR/cw" --version', workflow)
        self.assertIn('& (Join-Path $bin "cw.cmd") --version', workflow)
        self.assertIn("cw --version", workflow)

    def test_version_is_single_sourced_consistently(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        package = (ROOT / "cw/__init__.py").read_text(encoding="utf-8")
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('parent.parent / "VERSION"', package)
        self.assertIn('version = {file = ["VERSION"]}', metadata)
        self.assertEqual(version, __import__("cw").__version__)

    def test_safe_yaml_parser_is_a_constrained_runtime_dependency(self) -> None:
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dependencies = ["PyYAML>=6.0.2,<7"]', metadata)


if __name__ == "__main__":
    unittest.main()
