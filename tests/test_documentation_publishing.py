from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DocumentationPublishingTests(unittest.TestCase):
    def test_read_the_docs_uses_the_strict_pinned_mkdocs_build(self):
        configuration = (ROOT / ".readthedocs.yaml").read_text(encoding="utf-8")
        self.assertRegex(configuration, r"(?m)^version:\s*2\s*$")
        self.assertIn("os: ubuntu-24.04", configuration)
        self.assertIn('python: "3.13"', configuration)
        self.assertIn("configuration: mkdocs.yml", configuration)
        self.assertIn("fail_on_warning: true", configuration)
        self.assertIn("requirements: docs/requirements.txt", configuration)

    def test_mkdocs_uses_the_canonical_docs_domain_and_repository(self):
        configuration = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        self.assertRegex(
            configuration,
            r"(?m)^site_url:\s*https://docs\.cwcli\.dev\s*$",
        )
        self.assertRegex(
            configuration,
            r"(?m)^repo_url:\s*https://github\.com/Queopius/cw\s*$",
        )
        self.assertIn("logo: assets/brand/cw-mark.png", configuration)
        self.assertIn("favicon: assets/brand/cw-mark-32.png", configuration)
        self.assertTrue((ROOT / "docs/assets/brand/cw-mark.png").is_file())
        self.assertTrue((ROOT / "docs/assets/brand/cw-mark-32.png").is_file())
        self.assertRegex(configuration, r"(?m)^site_author:\s*Fantomid LLC\s*$")

    def test_plugin_public_metadata_uses_live_nonlegal_destinations(self):
        manifest = (ROOT / "plugins/cw/.codex-plugin/plugin.json").read_text(encoding="utf-8")
        listing = (ROOT / "docs/plugin-listing-draft.md").read_text(encoding="utf-8")
        combined = manifest + listing
        self.assertIn("https://docs.cwcli.dev/en/stable/plugin-app-candidate/", combined)
        self.assertIn("https://docs.cwcli.dev/en/stable/plugin-support/", listing)
        for broken in (
            "https://docs.cwcli.dev/plugin-app-candidate/",
            "https://docs.cwcli.dev/plugin-privacy/",
            "https://docs.cwcli.dev/plugin-support/",
            "https://docs.cwcli.dev/remote-auth/",
        ):
            self.assertNotIn(broken, combined)
        self.assertNotIn("privacyPolicyURL", manifest)
        self.assertNotIn("termsOfServiceURL", manifest)

    def test_package_metadata_keeps_semantic_public_destinations(self):
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('Homepage = "https://cwcli.dev"', metadata)
        self.assertIn('Documentation = "https://docs.cwcli.dev"', metadata)
        self.assertIn('Repository = "https://github.com/Queopius/cw.git"', metadata)
        self.assertIn('Issues = "https://github.com/Queopius/cw/issues"', metadata)
        stale_homepage = 'Homepage = "https://' + 'github.com/Queopius/cw"'
        self.assertNotIn(stale_homepage, metadata)

    def test_documentation_dependencies_remain_pinned_and_isolated(self):
        requirements = (ROOT / "docs/requirements.txt").read_text(encoding="utf-8")
        dependencies = [line for line in requirements.splitlines() if line.strip()]
        self.assertTrue(dependencies)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9-]+==[^=\s]+", line) for line in dependencies))

        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        runtime_block = metadata.split("dependencies =", 1)[1].split("[project.urls]", 1)[0]
        self.assertNotIn("mkdocs", runtime_block.lower())

    def test_ci_preserves_matrix_and_runs_one_strict_docs_job(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn('["3.10", "3.11", "3.12", "3.13", "3.14"]', workflow)
        self.assertEqual(1, workflow.count("  docs:\n"))
        self.assertIn('python-version: "3.13"', workflow)
        self.assertIn("run: mkdocs build --strict", workflow)


if __name__ == "__main__":
    unittest.main()
