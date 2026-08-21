#!/usr/bin/env python3
"""Validate the local CW plugin candidate without network or OpenAI services."""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "cw"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MCP = PLUGIN / ".mcp.json"
CAPABILITIES = PLUGIN / "capabilities.json"
PLUGIN_VERSION = PLUGIN / "VERSION"
SKILL = PLUGIN / "skills" / "cw-workflow" / "SKILL.md"
SKILL_UI = PLUGIN / "skills" / "cw-workflow" / "agents" / "openai.yaml"
PLUGIN_README = PLUGIN / "README.md"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
CONTRACT = ROOT / "docs" / "chatgpt-development-completion-contract.json"

ALLOWED_MANIFEST_FIELDS = {
    "name", "version", "description", "author", "homepage", "repository",
    "license", "keywords", "skills", "mcpServers", "interface",
}
ALLOWED_INTERFACE_FIELDS = {
    "displayName", "shortDescription", "longDescription", "developerName",
    "category", "capabilities", "websiteURL", "privacyPolicyURL",
    "termsOfServiceURL", "defaultPrompt", "brandColor", "composerIcon",
    "logo", "logoDark",
}
EXACT_TOOLS = {
    "cw_project_status", "cw_project_inspect", "cw_history", "cw_explain",
    "cw_completion_status", "cw_gate_status", "cw_phase_start", "cw_validate",
    "cw_request_review", "cw_retry", "cw_operation_status", "cw_operation_cancel",
}
EXPECTED_GROUPS = {
    "READ": {
        "cw_project_status", "cw_project_inspect", "cw_history", "cw_explain",
        "cw_completion_status", "cw_gate_status", "cw_operation_status",
    },
    "EXECUTION": {"cw_validate", "cw_request_review"},
    "CONTROLLED_STATE_MUTATION": {
        "cw_phase_start", "cw_retry", "cw_operation_cancel",
    },
}
FORBIDDEN_TERMS = {
    "cw_execute", "shell(command", "filesystem_read", "git(command",
    "cw_authorize_extension", "cw_create_gate", "cw_approve_gate",
    "cw_repair", "cw_rebaseline", "cw_release", "cw_deploy",
}
REQUIRED_CONTRACT_IDS = {
    "official-development-model", "real-chatgpt-connection", "project-scoping",
    "read-acceptance", "surface-classification", "controlled-action-acceptance",
    "high-consequence-exclusion", "security-privacy", "replay-recovery",
    "development-documentation", "future-auth-architecture",
}
EXPECTED_DISTRIBUTION_STATUS = {
    "local_mcp_stdio": "IMPLEMENTED",
    "staging_mcp_https": "IMPLEMENTED_FOR_TESTING",
    "staging_oauth_discovery": "IMPLEMENTED_FOR_TESTING",
    "production_mcp_https": "NOT_DEPLOYED",
    "production_oauth": "NOT_DEPLOYED",
    "openai_domain_verification": "NOT_COMPLETED",
    "universal_submission": "NOT_CREATED",
    "public_plugin_publication": "NOT_COMPLETED",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _local_path(base: Path, value: str) -> Path:
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes plugin package: {value}") from exc
    return candidate


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(?P<body>.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise ValueError("SKILL.md requires YAML frontmatter")
    values: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"invalid SKILL.md frontmatter line: {line}")
        values[key.strip()] = value.strip()
    return values


def validation_errors(root: Path = ROOT) -> list[str]:
    plugin = root / "plugins" / "cw"
    manifest_path = plugin / ".codex-plugin" / "plugin.json"
    mcp_path = plugin / ".mcp.json"
    capability_path = plugin / "capabilities.json"
    skill_path = plugin / "skills" / "cw-workflow" / "SKILL.md"
    skill_ui_path = plugin / "skills" / "cw-workflow" / "agents" / "openai.yaml"
    plugin_readme_path = plugin / "README.md"
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    contract_path = root / "docs" / "chatgpt-development-completion-contract.json"
    acceptance_path = root / "docs" / "chatgpt-development-acceptance.json"
    acceptance_evidence_path = root / "docs" / "acceptance" / "chatgpt-development-0.11.md"
    plugin_version_path = plugin / "VERSION"
    compatibility_policy_path = root / "cw" / "adapters" / "mcp" / "plugin-compatibility.json"
    license_path = root / "LICENSE"
    notice_path = root / "NOTICE"
    candidate_docs = (
        root / "docs" / "plugin-app-candidate.md",
        root / "docs" / "plugin-listing-draft.md",
        root / "docs" / "plugin-privacy.md",
        root / "docs" / "plugin-support.md",
        root / "docs" / "plugin-security.md",
        root / "docs" / "chatgpt-development.md",
        acceptance_path,
        acceptance_evidence_path,
        root / "docs" / "adr" / "0003-plugin-candidate.md",
        root / "docs" / "adr" / "0004-chatgpt-development-and-public-runtime.md",
    )
    errors: list[str] = []

    required = (
        manifest_path, mcp_path, capability_path, skill_path, skill_ui_path,
        plugin_readme_path,
        marketplace_path, contract_path, compatibility_policy_path,
        license_path, notice_path, *candidate_docs,
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        return [f"missing required plugin file: {item}" for item in missing]

    try:
        manifest = _load(manifest_path)
        mcp = _load(mcp_path)
        capabilities = _load(capability_path)
        marketplace = _load(marketplace_path)
        contract = _load(contract_path)
        acceptance = _load(acceptance_path)
        runtime_policy = _load(compatibility_policy_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid plugin JSON: {exc}"]
    expected_policy_fields = {"schema_version", "policy_id", "plugin_version", "core", "remote_protocol"}
    runtime_core = runtime_policy.get("core") if isinstance(runtime_policy, dict) else None
    if (
        not isinstance(runtime_policy, dict)
        or set(runtime_policy) != expected_policy_fields
        or runtime_policy.get("schema_version") != 1
        or runtime_policy.get("policy_id") != "cw.plugin.compatibility.v1"
        or not isinstance(runtime_core, dict)
        or set(runtime_core) != {"minimum", "maximum_exclusive"}
        or not all(
            isinstance(runtime_core.get(key), str)
            and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", runtime_core[key])
            for key in ("minimum", "maximum_exclusive")
        )
    ):
        errors.append("runtime Plugin compatibility policy is invalid")
    elif tuple(map(int, runtime_core["minimum"].split("."))) >= tuple(
        map(int, runtime_core["maximum_exclusive"].split("."))
    ):
        errors.append("runtime Plugin compatibility policy range is invalid")

    core_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    try:
        version_stat = plugin_version_path.lstat()
        if stat.S_ISLNK(version_stat.st_mode) or not stat.S_ISREG(version_stat.st_mode):
            raise OSError("plugins/cw/VERSION must be a regular non-symlink file")
        plugin_version = plugin_version_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        errors.append(f"plugin VERSION file is missing: {exc}")
        plugin_version = ""
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", plugin_version):
        errors.append("plugins/cw/VERSION must contain a valid stable semantic version")
    manifest_version = str(manifest.get("version", ""))
    if manifest_version.split("+", 1)[0] != plugin_version:
        errors.append("plugin manifest base version must equal plugins/cw/VERSION")
    compatibility = capabilities.get("compatibility", {})
    if not isinstance(compatibility, dict):
        errors.append("capability compatibility block must be an object")
        compatibility = {}
    else:
        compat_plugin_version = str(compatibility.get("plugin_version", ""))
        if compat_plugin_version != plugin_version:
            errors.append("compatibility.plugin_version must match plugins/cw/VERSION")
        cw_core = compatibility.get("cw_core", {})
        if not isinstance(cw_core, dict):
            errors.append("compatibility.cw_core must be an object")
        else:
            runtime_core = runtime_policy.get("core", {}) if isinstance(runtime_policy, dict) else {}
            expected_policy = (
                f">={runtime_core.get('minimum', '')},"
                f"<{runtime_core.get('maximum_exclusive', '')}"
            )
            if str(cw_core.get("minimum", "")) != runtime_core.get("minimum"):
                errors.append("compatibility.cw_core.minimum must match the runtime policy")
            if cw_core.get("compatible_policy") != expected_policy:
                errors.append("compatibility.cw_core.compatible_policy must match the runtime policy")
        remote_protocol = compatibility.get("remote_protocol", {})
        if not isinstance(remote_protocol, dict):
            errors.append("compatibility.remote_protocol must be an object")
        else:
            if str(remote_protocol.get("required", "")) != "cw.remote.v1":
                errors.append("compatibility.remote_protocol.required must be cw.remote.v1")
    if not isinstance(runtime_policy, dict) or runtime_policy.get("plugin_version") != plugin_version:
        errors.append("runtime Plugin compatibility policy must match plugins/cw/VERSION")
    if not isinstance(runtime_policy, dict) or runtime_policy.get("remote_protocol") != "cw.remote.v1":
        errors.append("runtime Plugin compatibility policy must require cw.remote.v1")
    for legal_path in (license_path, notice_path):
        try:
            legal_stat = legal_path.lstat()
        except OSError as exc:
            errors.append(f"canonical legal file is missing: {exc}")
        else:
            if stat.S_ISLNK(legal_stat.st_mode) or not stat.S_ISREG(legal_stat.st_mode):
                errors.append(f"canonical legal file must be regular and non-symlink: {legal_path.name}")
    if contract.get("milestone_version") != "0.11.0":
        errors.append("ChatGPT development Completion Contract must remain historical 0.11 evidence")
    if manifest.get("name") != "cw":
        errors.append("plugin name must be cw")
    if set(manifest) - ALLOWED_MANIFEST_FIELDS:
        errors.append("plugin manifest contains unsupported top-level fields")
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        errors.append("plugin interface must be an object")
        interface = {}
    if set(interface) - ALLOWED_INTERFACE_FIELDS:
        errors.append("plugin interface contains unsupported fields")
    if interface.get("displayName") != "CW — Codex Workflow":
        errors.append("plugin displayName must preserve the CW product identity")
    if manifest.get("author") != {"name": "Fantomid LLC", "url": "https://cwcli.dev"}:
        errors.append("plugin author must identify Fantomid LLC at the canonical product website")
    if interface.get("developerName") != "Queopius | Fantomid LLC":
        errors.append("plugin developerName must preserve the Queopius and Fantomid LLC identity")
    if manifest.get("homepage") != "https://docs.cwcli.dev/en/stable/plugin-app-candidate/":
        errors.append("plugin homepage must use the live canonical Plugin documentation URL")
    for key in ("homepage", "repository"):
        if not str(manifest.get(key, "")).startswith("https://"):
            errors.append(f"plugin {key} must use HTTPS")
    if interface.get("websiteURL") != "https://cwcli.dev":
        errors.append("plugin interface websiteURL must use the canonical product website")
    if "privacyPolicyURL" in interface or "termsOfServiceURL" in interface:
        errors.append("draft legal documents must not be linked as final Plugin policies")
    prompts = interface.get("defaultPrompt")
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 3:
        errors.append("plugin interface requires one to three default prompts")
    elif any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in prompts):
        errors.append("default prompts must be non-empty strings up to 128 characters")

    for key in ("skills", "mcpServers"):
        value = manifest.get(key)
        if not isinstance(value, str):
            errors.append(f"plugin {key} must be a package-relative path")
            continue
        try:
            target = _local_path(plugin, value)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if not target.exists():
                errors.append(f"plugin {key} target does not exist")
    for key in ("composerIcon", "logo", "logoDark"):
        value = interface.get(key)
        if not isinstance(value, str):
            errors.append(f"plugin interface {key} must be a package-relative path")
            continue
        try:
            target = _local_path(plugin, value)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if not target.is_file() or target.suffix.lower() != ".png":
                errors.append(f"plugin interface {key} must reference a PNG asset")

    expected_mcp = {
        "mcpServers": {
            "cw": {
                "command": "cw",
                "args": ["mcp", "serve", "--allowed-root", ".", "--project", "."],
            }
        }
    }
    if mcp != expected_mcp:
        errors.append("plugin MCP definition must invoke only the scoped CW stdio runtime")

    exposed = capabilities.get("exposed")
    if not isinstance(exposed, dict):
        errors.append("plugin capability map must contain exposed groups")
        exposed = {}
    normalized_groups = {
        key: set(value) if isinstance(value, list) else set()
        for key, value in exposed.items()
    }
    if normalized_groups != EXPECTED_GROUPS:
        errors.append("plugin capability groups do not match the accepted CW 0.9 surface")
    all_tools = set().union(*normalized_groups.values()) if normalized_groups else set()
    if all_tools != EXACT_TOOLS:
        errors.append("plugin capability map does not expose the exact accepted tool set")
    if capabilities.get("enforcement") != (
        "Server-side CWApplication policy is authoritative; plugin metadata and tool annotations are advisory."
    ):
        errors.append("plugin capability map must declare server-side enforcement authority")
    chatgpt_development = capabilities.get("chatgpt_development")
    if not isinstance(chatgpt_development, dict) or chatgpt_development.get("default_profile") != "read-only":
        errors.append("plugin capability map must default ChatGPT development to read-only")
    elif set(chatgpt_development.get("profiles", {}).get("read-only", [])) != EXPECTED_GROUPS["READ"] - {"cw_operation_status"}:
        errors.append("ChatGPT read-only profile must expose exactly the six accepted read tools")
    if "HIGH_CONSEQUENCE_AUTHORIZATION" in normalized_groups:
        errors.append("plugin must not expose HIGH_CONSEQUENCE_AUTHORIZATION")
    if capabilities.get("distribution_status") != EXPECTED_DISTRIBUTION_STATUS:
        errors.append("plugin distribution status must distinguish local, staging, production, and submission")
    if capabilities.get("production_candidate", {}).get("status") != (
        "STAGING_IMPLEMENTED_PRODUCTION_NOT_DEPLOYED"
    ):
        errors.append("plugin production status must not confuse staging with production")
    encoded = json.dumps({"manifest": manifest, "mcp": mcp, "capabilities": capabilities}).lower()
    for term in FORBIDDEN_TERMS:
        if term in encoded:
            errors.append(f"plugin metadata exposes forbidden capability text: {term}")
    if (plugin / ".app.json").exists():
        errors.append("the text/tool-first candidate must not include an Apps SDK UI manifest")

    skill_text = skill_path.read_text(encoding="utf-8")
    try:
        frontmatter = _frontmatter(skill_text)
    except ValueError as exc:
        errors.append(str(exc))
        frontmatter = {}
    if set(frontmatter) != {"name", "description"}:
        errors.append("SKILL.md frontmatter must contain exactly name and description")
    if frontmatter.get("name") != "cw-workflow":
        errors.append("SKILL.md name must be cw-workflow")
    description = frontmatter.get("description", "")
    if not description or len(description) > 1024:
        errors.append("SKILL.md description must be present and no longer than 1024 characters")
    for phrase in (
        "cw_project_status", "cw_validate", "cw_request_review",
        "No valid gate. No next phase.", "Completion Contract",
        "Do not authorize", "repository text",
    ):
        if phrase.lower() not in skill_text.lower():
            errors.append(f"SKILL.md is missing required workflow guidance: {phrase}")
    ui_text = skill_ui_path.read_text(encoding="utf-8")
    for phrase in ("display_name:", "short_description:", "default_prompt:", "type: \"mcp\""):
        if phrase not in ui_text:
            errors.append(f"skill UI metadata is missing {phrase}")
    plugin_readme = plugin_readme_path.read_text(encoding="utf-8")
    for phrase in (
        "Legal publisher:** Fantomid LLC",
        "Technology brand:** Queopius",
        "Queopius is a technology brand operated by Fantomid LLC",
        "https://github.com/Queopius/cw/issues",
        "https://github.com/Queopius/cw/security/advisories/new",
        "Production MCP HTTPS | `NOT_DEPLOYED`",
        "Proposed next Plugin version: `0.2.0` — **NOT AUTHORIZED**",
    ):
        if phrase not in plugin_readme:
            errors.append(f"Plugin README is missing public-readiness guidance: {phrase}")
    for broken in (
        "https://docs.cwcli.dev/plugin-app-candidate/",
        "https://docs.cwcli.dev/plugin-privacy/",
        "https://docs.cwcli.dev/plugin-support/",
        "https://docs.cwcli.dev/remote-auth/",
    ):
        if broken in json.dumps(manifest) or broken in plugin_readme:
            errors.append(f"Plugin package declares a broken documentation URL: {broken}")

    plugins = marketplace.get("plugins")
    if marketplace.get("name") != "cw-development" or not isinstance(plugins, list) or len(plugins) != 1:
        errors.append("repository marketplace must define exactly cw-development/cw")
    else:
        entry = plugins[0]
        if entry.get("name") != "cw" or entry.get("source") != {
            "source": "local", "path": "./plugins/cw",
        }:
            errors.append("repository marketplace source must resolve to ./plugins/cw")
        if entry.get("policy") != {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}:
            errors.append("repository marketplace must require explicit available/on-install enablement")

    completion_target = contract.get("completion_target")
    if not isinstance(completion_target, dict):
        errors.append("plugin candidate must define a native CW completion_target")
        completion_target = {}
    if set(completion_target) != {"id", "name", "description", "target_type", "requirements"}:
        errors.append("plugin candidate completion_target does not match the CW contract fields")
    requirements = completion_target.get("requirements")
    ids = {
        item.get("id") for item in requirements if isinstance(item, dict)
    } if isinstance(requirements, list) else set()
    if ids != REQUIRED_CONTRACT_IDS:
        errors.append("plugin candidate Completion Contract requirements are incomplete")
    if isinstance(requirements, list):
        for requirement in requirements:
            if not isinstance(requirement, dict) or set(requirement) != {
                "id", "description", "severity", "evidence_expectations", "project_specific",
            }:
                errors.append("plugin Completion Contract requirement has invalid CW fields")
                continue
            expected_severity = (
                "advisory"
                if requirement.get("id") == "controlled-action-acceptance"
                else "blocking"
            )
            if requirement.get("severity") != expected_severity:
                errors.append(
                    "ChatGPT Completion Contract severity does not match its platform-conditional policy"
                )
            evidence = requirement.get("evidence_expectations")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence
            ):
                errors.append("plugin Completion Contract requirement needs evidence expectations")
    if contract.get("submission_in_scope") is not False:
        errors.append("plugin candidate must not require or imply public submission")

    manual = acceptance.get("manual_chatgpt", {})
    if (
        acceptance.get("completion_decision") != "SATISFIED"
        or manual.get("status") != "PASS"
        or manual.get("surface") != "read-only"
        or manual.get("forbidden_mutation") != "PASS"
        or manual.get("human_gate_integrity") != "PASS"
        or acceptance.get("secrets_recorded") is not False
    ):
        errors.append("ChatGPT 0.11 real read-only acceptance evidence is incomplete")

    candidate_text = re.sub(
        r"\s+", " ", "\n".join(
            path.read_text(encoding="utf-8") for path in candidate_docs
        ),
    )
    for phrase in (
        "developers.openai.com/plugins/build/plugins",
        "developers.openai.com/plugins/build/mcp-server",
        "developers.openai.com/plugins/build/auth",
        "developers.openai.com/plugins/deploy/submission",
        "developers.openai.com/plugins/guides/security-privacy",
        "developers.openai.com/api/docs/guides/secure-mcp-tunnels",
        "PLATFORM_CAPABILITY_UNAVAILABLE",
        "Tunnels Read + Use",
        "NOT_TESTED_BY_DESIGN",
        "NEEDS_HUMAN_BUSINESS_INPUT",
        "authenticated relay",
    ):
        if phrase not in candidate_text:
            errors.append(f"plugin surface/remote classification docs are missing: {phrase}")

    for path in sorted(plugin.rglob("*")):
        if path.is_symlink():
            errors.append(f"plugin package must not contain symlinks: {path.relative_to(root)}")
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in {".ttf", ".otf", ".woff", ".woff2"}:
            errors.append(f"plugin package must not include source fonts: {path.name}")
        if path.suffix.lower() in {".json", ".md", ".yaml", ".yml"}:
            text = path.read_text(encoding="utf-8")
            if "TODO" in text or "/home/" in text or "C:\\Users\\" in text:
                errors.append(f"plugin package contains placeholder or private path: {path.relative_to(root)}")
    return errors


def main() -> int:
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("CW plugin candidate package, skill, capabilities, and Completion Contract are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
