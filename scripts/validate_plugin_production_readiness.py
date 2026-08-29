#!/usr/bin/env python3
"""Validate CW 0.12 production-readiness contracts without a remote service."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from .validate_plugin_candidate import (
        EXACT_TOOLS, PLUGIN, ROOT, validation_errors as plugin_errors,
    )
except ImportError:  # Direct script execution keeps scripts/ on sys.path.
    from validate_plugin_candidate import (
        EXACT_TOOLS, PLUGIN, ROOT, validation_errors as plugin_errors,
    )


CONTRACT = ROOT / "docs" / "plugin-production-completion-contract.json"
EVIDENCE = ROOT / "docs" / "plugin-production-readiness-evidence.json"
CAPABILITIES = PLUGIN / "capabilities.json"
REQUIRED_DOCS = (
    ROOT / "docs" / "plugin-production-readiness.md",
    ROOT / "docs" / "plugin-auth.md",
    ROOT / "docs" / "plugin-privacy.md",
    ROOT / "docs" / "plugin-security.md",
    ROOT / "docs" / "plugin-deployment.md",
    ROOT / "docs" / "plugin-submission.md",
    ROOT / "docs" / "acceptance" / "chatgpt-plugin-0.12.md",
    ROOT / "docs" / "adr" / "0005-production-mcp-relay.md",
)
REQUIRED_CONTRACT_IDS = {
    "official-platform-model", "plugin-package", "production-topology",
    "authentication", "authorization", "controlled-actions", "security",
    "privacy-data-flow", "operations", "submission-artifacts",
    "compatibility", "external-production-acceptance",
}
EXPECTED_SCOPES = {
    "project.read", "gate.read", "history.read", "completion.read",
    "operation.read", "validation.execute", "review.execute", "phase.start",
    "retry.execute", "operation.cancel",
}
HIGH_CONSEQUENCE_BINDINGS = {
    "concrete_action_binding", "project_binding", "proposal_or_evidence_digest",
    "typed_human_principal", "short_expiry", "single_use_nonce",
    "auditable_consumption",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validation_errors(root: Path = ROOT) -> list[str]:
    errors = [f"base plugin: {item}" for item in plugin_errors(root)]
    required = tuple(root / path.relative_to(ROOT) for path in (CONTRACT, EVIDENCE, *REQUIRED_DOCS))
    for path in required:
        if not path.is_file():
            errors.append(f"missing production-readiness artifact: {path.relative_to(root)}")
    if errors:
        return errors

    contract = _load(root / CONTRACT.relative_to(ROOT))
    evidence = _load(root / EVIDENCE.relative_to(ROOT))
    capabilities = _load(root / CAPABILITIES.relative_to(ROOT))
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    if contract.get("milestone_version") != "0.12.0":
        errors.append("the accepted production-readiness baseline must remain CW 0.12.0")
    try:
        current_parts = tuple(int(item) for item in version.split("."))
    except ValueError:
        current_parts = ()
    if len(current_parts) != 3 or current_parts < (0, 12, 0):
        errors.append("current VERSION must retain compatibility with the accepted CW 0.12 baseline")
    target = contract.get("completion_target", {})
    requirements = target.get("requirements", [])
    ids = {item.get("id") for item in requirements if isinstance(item, dict)}
    if target.get("target_type") != "plugin-production-readiness" or ids != REQUIRED_CONTRACT_IDS:
        errors.append("production Completion Contract requirements are incomplete")
    if contract.get("submission_in_scope") is not False:
        errors.append("CW 0.12 must not authorize public submission")
    for item in requirements:
        expected = "advisory" if item.get("id") == "external-production-acceptance" else "blocking"
        if item.get("severity") != expected or not item.get("evidence_expectations"):
            errors.append(f"invalid production requirement semantics: {item.get('id')}")

    production = capabilities.get("production_candidate", {})
    scopes = production.get("scopes", {})
    flattened = {
        scope for values in scopes.values() if isinstance(values, list) for scope in values
    }
    if flattened != EXPECTED_SCOPES or "workflow.admin" in flattened:
        errors.append("production OAuth scopes do not match narrow CW capabilities")
    high = production.get("high_consequence_authorization", {})
    if (
        high.get("exposed") is not False
        or high.get("oauth_scope_is_sufficient") is not False
        or set(high.get("requires", [])) != HIGH_CONSEQUENCE_BINDINGS
    ):
        errors.append("high-consequence authorization ceremony is not safely separated")
    surface = production.get("surface_policy", {})
    if set(surface) != {"chatgpt_pro", "business_enterprise", "unknown_surface"}:
        errors.append("surface policy must explicitly cover Pro, managed workspaces, and unknown clients")
    tokens = production.get("token_policy", {})
    if (
        tokens.get("pkce_method") != "S256"
        or tokens.get("access_token_max_seconds", 10**9) > 600
        or tokens.get("authorization_code_max_seconds", 10**9) > 300
        or tokens.get("refresh_token_rotation") != "required"
        or tokens.get("refresh_token_absolute_max_seconds", 10**9) > 2592000
        or tokens.get("revocation_check") != "every_request"
        or high.get("max_ttl_seconds", 10**9) > 300
    ):
        errors.append("OAuth/high-consequence token expiry and rotation policy is incomplete")

    declared = {
        tool
        for values in capabilities.get("exposed", {}).values()
        if isinstance(values, list)
        for tool in values
    }
    if declared != EXACT_TOOLS:
        errors.append("CW 0.12 changed the accepted MCP tool surface")
    if (root / "plugins" / "cw" / ".app.json").exists():
        errors.append("remote .app.json must not exist before a real registered endpoint")

    if (
        evidence.get("production_readiness") != "NOT_READY"
        or evidence.get("plugin_submission_readiness") != "BLOCKED"
        or evidence.get("secrets_recorded") is not False
        or evidence.get("implemented_in_0_12", {}).get("public_submission") is not False
    ):
        errors.append("production evidence must truthfully preserve undeployed/blocked status")
    acceptance = evidence.get("technical_acceptance", {})
    github = acceptance.get("github", {})
    matrix = acceptance.get("native_matrix", {})
    accepted_sha = acceptance.get("accepted_candidate_sha")
    if (
        acceptance.get("status") != "ACCEPTED"
        or not re.fullmatch(r"[0-9a-f]{40}", str(accepted_sha))
        or set(matrix) != {
            "linux_x86_64", "windows_x86_64", "macos_arm64", "macos_intel",
        }
        or set(matrix.values()) != {"PASS"}
        or any(
            github.get(run, {}).get("status") != "PASS"
            or github.get(run, {}).get("sha") != accepted_sha
            or not str(github.get(run, {}).get("url", "")).startswith(
                "https://github.com/Queopius/cw/actions/runs/"
            )
            for run in ("ci", "platform_acceptance")
        )
    ):
        errors.append("technical acceptance evidence is incomplete or not exact-SHA")

    docs_text = "\n".join(path.read_text(encoding="utf-8") for path in required)
    for phrase in (
        "2026-08-15",
        "developers.openai.com/plugins/build/plugins",
        "developers.openai.com/plugins/build/mcp-server",
        "developers.openai.com/plugins/build/auth",
        "developers.openai.com/plugins/deploy/submission",
        "developers.openai.com/plugins/guides/security-privacy",
        "developers.openai.com/api/docs/guides/secure-mcp-tunnels",
        "OAuth 2.1",
        "public streamable-HTTPS",
        "HIGH_CONSEQUENCE_AUTHORIZATION",
        "PLUGIN SUBMISSION READINESS: BLOCKED",
    ):
        if phrase not in docs_text:
            errors.append(f"production-readiness docs are missing: {phrase}")
    if re.search(r"/home/[^\s`]+|C:\\Users\\|sk-[A-Za-z0-9]", docs_text):
        errors.append("production-readiness artifacts contain a private path or secret-shaped value")
    return errors


def main() -> int:
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("CW 0.12 plugin production-readiness baseline contracts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
