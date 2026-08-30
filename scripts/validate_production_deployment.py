#!/usr/bin/env python3
"""Validate the non-deploying CW Production EAP infrastructure contract."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "render.production.yaml"
CONTRACT = ROOT / "config" / "production-environment.json"
STAGING_BLUEPRINT = ROOT / "render.yaml"
PRODUCTION_DOCS = (
    ROOT / "docs" / "operations" / "production-eap.md",
    ROOT / "docs" / "plugin-deployment.md",
)

EXPECTED_VALUES = {
    "CW_DEPLOYMENT_ENV": "production",
    "CW_PLUGIN_VERSION": "0.1.0",
    "CW_GATEWAY_RESOURCE_URL": "https://mcp.cwcli.dev/mcp",
    "CW_GATEWAY_DATABASE": "/var/lib/cw/gateway.sqlite3",
    "CW_GATEWAY_HOST": "0.0.0.0",
    "CW_GATEWAY_DOCUMENTATION_URL": "https://docs.cwcli.dev/en/stable/remote-auth/",
    "CW_OAUTH_ISSUER_URL": "https://auth.cwcli.dev/",
    "CW_OAUTH_JWKS_URL": "https://auth.cwcli.dev/.well-known/jwks.json",
    "CW_OAUTH_WORKSPACE_CLAIM": "https://cwcli.dev/claims/workspace",
    "CW_OAUTH_ALGORITHMS": "RS256",
    "CW_PAIRING_WEB_REDIRECT_URI": "https://mcp.cwcli.dev/remote/pair/callback",
    "CW_LIMIT_REQUESTS_PER_MINUTE": "120",
    "CW_LIMIT_DEVICE_REQUESTS_PER_MINUTE": "240",
    "CW_LIMIT_PAIRING_REQUESTS_PER_MINUTE": "20",
    "CW_LIMIT_CONCURRENT_PER_DEVICE": "4",
    "CW_LIMIT_REQUEST_BYTES": "65536",
    "CW_LIMIT_AGENT_MESSAGE_BYTES": "524288",
    "CW_LIMIT_OPERATION_TIMEOUT_SECONDS": "30",
    "CW_LIMIT_AGENT_IDLE_SECONDS": "45",
    "CW_LIMIT_COMPLETED_CACHE": "1024",
}
EXTERNAL_KEYS = {
    "CW_GATEWAY_ALLOWED_HOSTS",
    "CW_PAIRING_WEB_CLIENT_ID",
    "CW_PAIRING_SESSION_SECRET",
}
FORBIDDEN_PRODUCTION_IDENTITIES = (
    "staging-mcp.cwcli.dev",
    "login.cwcli.dev",
    "cw-staging",
    "cw-staging-mcp",
    "cw-staging-data",
)
SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{20,}", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\brnd_[A-Za-z0-9_-]{16,}\b", re.I),
)


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env_map(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = service.get("envVars", [])
    return {str(item.get("key")): item for item in entries if isinstance(item, dict)}


def validation_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    blueprint_path = root / BLUEPRINT.relative_to(ROOT)
    contract_path = root / CONTRACT.relative_to(ROOT)
    staging_path = root / STAGING_BLUEPRINT.relative_to(ROOT)
    docs = tuple(root / path.relative_to(ROOT) for path in PRODUCTION_DOCS)
    for path in (blueprint_path, contract_path, staging_path, root / "Dockerfile", *docs):
        if not path.is_file():
            errors.append(f"missing deployment artifact: {path.relative_to(root)}")
    if errors:
        return errors

    try:
        blueprint = _load_yaml(blueprint_path)
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        staging = _load_yaml(staging_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [f"invalid production deployment data: {exc}"]

    services = blueprint.get("services", []) if isinstance(blueprint, dict) else []
    if not isinstance(services, list) or len(services) != 1 or not isinstance(services[0], dict):
        return ["production Blueprint must define exactly one service"]
    service = services[0]
    expected_service = {
        "type": "web", "name": "cw-mcp", "runtime": "docker",
        "dockerfilePath": "./Dockerfile", "branch": "prod", "region": "frankfurt",
        "plan": "starter", "numInstances": 1, "autoDeployTrigger": "checksPass",
        "healthCheckPath": "/readyz", "domains": ["mcp.cwcli.dev"],
    }
    for key, expected in expected_service.items():
        if service.get(key) != expected:
            errors.append(f"production Blueprint has invalid {key}")
    if service.get("disk") != {
        "name": "cw-production-data", "mountPath": "/var/lib/cw", "sizeGB": 1,
    }:
        errors.append("production Blueprint disk contract is invalid")

    env = _env_map(service)
    for name, expected in EXPECTED_VALUES.items():
        if env.get(name) != {"key": name, "value": expected}:
            errors.append(f"production Blueprint has invalid binding: {name}")
    for name in EXTERNAL_KEYS:
        if env.get(name) != {"key": name, "sync": False}:
            errors.append(f"production Blueprint must externally manage: {name}")
    if "CW_BUILD_SHA" in env:
        errors.append("production must use Render's exact RENDER_GIT_COMMIT build identity")

    if contract.get("schema_version") != 1 or contract.get("environment") != "production":
        errors.append("production environment contract identity is invalid")
    if contract.get("auth0_region") != "US" or contract.get("initial_workspace") != "cw-production":
        errors.append("production Auth0 region/workspace contract is invalid")
    if contract.get("variables") != EXPECTED_VALUES:
        errors.append("production environment values do not match the canonical contract")
    if set(contract.get("externally_managed", [])) != EXTERNAL_KEYS | {"RENDER_GIT_COMMIT", "PORT"}:
        errors.append("production externally managed variables are invalid")
    if contract.get("secrets") != ["CW_PAIRING_SESSION_SECRET"]:
        errors.append("production gateway secret inventory is invalid")
    if contract.get("pairing_oauth") != {
        "client_type": "public", "pkce_method": "S256",
        "client_secret": False, "delegated_scope": "project.read",
    }:
        errors.append("production pairing OAuth contract is invalid")

    staging_service = staging.get("services", [{}])[0]
    if staging_service.get("name") != "cw-staging-mcp" or staging_service.get("branch") != "staging":
        errors.append("staging service identity changed")
    if staging_service.get("disk", {}).get("name") != "cw-staging-data":
        errors.append("staging disk identity changed")
    staging_env = _env_map(staging_service)
    if staging_env.get("CW_GATEWAY_RESOURCE_URL", {}).get("value") != "https://staging-mcp.cwcli.dev/mcp":
        errors.append("staging resource identity changed")
    if service["name"] == staging_service["name"] or service["disk"]["name"] == staging_service["disk"]["name"]:
        errors.append("production service or disk reuses staging")

    production_text = blueprint_path.read_text(encoding="utf-8") + contract_path.read_text(encoding="utf-8")
    lowered = production_text.lower()
    for value in FORBIDDEN_PRODUCTION_IDENTITIES:
        if value.lower() in lowered:
            errors.append(f"production configuration leaks a staging identity: {value}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(production_text + "\n" + "\n".join(path.read_text(encoding="utf-8") for path in docs)):
            errors.append(f"production configuration contains secret-shaped data: {pattern.pattern}")
    docs_text = "\n".join(path.read_text(encoding="utf-8") for path in docs)
    for phrase in (
        "single-instance and not highly available",
        "invite-only",
        "PRAGMA integrity_check",
        "SHA-256",
        "schema version",
        "deployed SHA",
        "isolated restore",
        "first-production rollback",
        "operator device revocation and individual project-grant revocation",
        "operationally BLOCKED",
    ):
        if phrase.lower() not in docs_text.lower():
            errors.append(f"production operations documentation is missing: {phrase}")
    if re.search(r"/home/[A-Za-z0-9._-]+/|C:\\Users\\", docs_text):
        errors.append("production operations documentation contains a private filesystem path")

    if (root / "VERSION").read_text(encoding="utf-8").strip() != "0.18.3":
        errors.append("production candidate Core must be 0.18.3")
    if (root / "plugins/cw/VERSION").read_text(encoding="utf-8").strip() != "0.1.0":
        errors.append("production candidate Plugin must be 0.1.0")
    protocol = (root / "cw/remote/protocol.py").read_text(encoding="utf-8")
    if 'PROTOCOL_VERSION = "cw.remote.v1"' not in protocol:
        errors.append("production candidate remote protocol must be cw.remote.v1")
    for legacy in ("cw-plugin-0.14.0.zip", "cw-plugin-0.10.0.zip", "cw-plugin-0.18.3.zip"):
        if legacy in production_text:
            errors.append(f"legacy/Core-coupled Plugin archive is not a production candidate: {legacy}")
    return errors


def main() -> int:
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(BLUEPRINT.read_bytes()).hexdigest()
    print(f"CW Production EAP deployment contract is valid (Blueprint SHA-256 {digest}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
