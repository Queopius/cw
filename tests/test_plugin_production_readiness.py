from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cw.adapters.mcp.runtime import TOOLS
from cw.application.capabilities import CAPABILITIES, CapabilityClass
from cw.core.models import CompletionContract
from scripts.build_plugin_candidate import build
from scripts.validate_plugin_production_readiness import (
    CONTRACT,
    EVIDENCE,
    EXPECTED_SCOPES,
    PLUGIN,
    REQUIRED_DOCS,
    validation_errors,
)


class PluginProductionReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capabilities = json.loads(
            (PLUGIN / "capabilities.json").read_text(encoding="utf-8")
        )
        self.production = self.capabilities["production_candidate"]

    def test_production_contract_and_evidence_validate(self) -> None:
        self.assertEqual([], validation_errors())
        document = json.loads(CONTRACT.read_text(encoding="utf-8"))
        contract = CompletionContract.from_dict(document["completion_target"])
        self.assertEqual("plugin-production-readiness", contract.target_type)
        self.assertEqual(12, len(contract.requirements))
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual("NOT_READY", evidence["production_readiness"])
        self.assertEqual("BLOCKED", evidence["plugin_submission_readiness"])
        self.assertFalse(evidence["secrets_recorded"])
        acceptance = evidence["technical_acceptance"]
        self.assertEqual("ACCEPTED", acceptance["status"])
        self.assertEqual(
            "45f89472a0d61effc6e1860960c3d3facf6f03cb",
            acceptance["accepted_candidate_sha"],
        )
        self.assertEqual(
            {"PASS"}, set(acceptance["native_matrix"].values())
        )
        self.assertEqual(
            {"PASS"},
            {
                acceptance["github"]["ci"]["status"],
                acceptance["github"]["platform_acceptance"]["status"],
            },
        )

    def test_oauth_scopes_are_narrow_and_match_runtime_capabilities(self) -> None:
        scopes = {
            scope
            for values in self.production["scopes"].values()
            for scope in values
        }
        runtime_allowed = set(CAPABILITIES) - {
            "project.repair", "extension.authorize", "plan.rebaseline",
        }
        self.assertEqual(EXPECTED_SCOPES, scopes)
        self.assertEqual(runtime_allowed, scopes)
        self.assertNotIn("workflow.admin", scopes)
        tokens = self.production["token_policy"]
        self.assertEqual("S256", tokens["pkce_method"])
        self.assertLessEqual(tokens["access_token_max_seconds"], 600)
        self.assertLessEqual(tokens["authorization_code_max_seconds"], 300)
        self.assertEqual("required", tokens["refresh_token_rotation"])
        self.assertLessEqual(tokens["refresh_token_absolute_max_seconds"], 2592000)
        self.assertEqual("every_request", tokens["revocation_check"])

    def test_high_consequence_remains_separate_and_unexposed(self) -> None:
        extension = CAPABILITIES["extension.authorize"]
        self.assertEqual(
            CapabilityClass.HIGH_CONSEQUENCE_AUTHORIZATION,
            extension.classification,
        )
        self.assertTrue(extension.human_authorization_required)
        high = self.production["high_consequence_authorization"]
        self.assertFalse(high["exposed"])
        self.assertFalse(high["oauth_scope_is_sufficient"])
        self.assertLessEqual(high["max_ttl_seconds"], 300)
        declared = {
            item for values in self.capabilities["exposed"].values() for item in values
        }
        runtime = {contract.name for contract in TOOLS}
        self.assertEqual(runtime, declared)
        self.assertFalse({
            "cw_authorize_extension", "cw_create_gate", "cw_approve_gate",
            "cw_repair", "cw_release", "cw_deploy",
        } & declared)

    def test_surface_policy_does_not_infer_write_access_from_plan_name(self) -> None:
        policy = self.production["surface_policy"]
        self.assertIn("read-only by default", policy["chatgpt_pro"])
        self.assertIn("runtime discovery", policy["chatgpt_pro"])
        self.assertIn("platform availability", policy["chatgpt_pro"])
        self.assertIn("workspace-admin opt-in", policy["business_enterprise"])
        self.assertEqual("read-only", policy["unknown_surface"])

    def test_skill_resists_injection_and_explains_human_review(self) -> None:
        skill = (PLUGIN / "skills/cw-workflow/SKILL.md").read_text(encoding="utf-8")
        for phrase in (
            "HUMAN_REVIEW_REQUIRED", "prompt injection",
            "ChatGPT confirmation is additional UI safety",
            "Never infer plan capabilities", "Do not authorize",
        ):
            self.assertIn(phrase.lower(), skill.lower())

    def test_production_artifacts_are_sanitized_and_package_is_deterministic(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8") for path in REQUIRED_DOCS)
        self.assertNotIn("/home/", text)
        self.assertNotIn("C:\\Users\\", text)
        self.assertNotIn("YOUR_TUNNEL_ID", text)
        self.assertFalse((PLUGIN / ".app.json").exists())
        with tempfile.TemporaryDirectory(prefix="cw-production-plugin-") as temporary:
            archive = Path(temporary) / "cw-plugin.zip"
            first = build(archive)
            first_bytes = archive.read_bytes()
            second = build(archive)
            self.assertEqual(first_bytes, archive.read_bytes())
            self.assertEqual(first["sha256"], second["sha256"])


if __name__ == "__main__":
    unittest.main()
