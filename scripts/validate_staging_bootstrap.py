#!/usr/bin/env python3
"""Validate deterministic CW 0.14 staging deployment contracts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCS = (
    "staging-environment.md",
    "auth0-staging.md",
    "acceptance/staging-bootstrap-0.14.md",
    "adr/0010-render-staging-hosting.md",
    "operations/staging-deploy.md",
    "operations/gateway-runbook.md",
    "operations/agent-runbook.md",
    "operations/oauth-runbook.md",
    "operations/incident-response.md",
    "operations/backup-restore.md",
    "operations/key-rotation.md",
)
REQUIRED_RENDER_KEYS = {
    "CW_DEPLOYMENT_ENV",
    "CW_GATEWAY_RESOURCE_URL",
    "CW_GATEWAY_DATABASE",
    "CW_GATEWAY_HOST",
    "CW_GATEWAY_ALLOWED_HOSTS",
    "CW_GATEWAY_DOCUMENTATION_URL",
    "CW_OAUTH_WORKSPACE_CLAIM",
    "CW_OAUTH_ALGORITHMS",
    "CW_OAUTH_ISSUER_URL",
    "CW_OAUTH_JWKS_URL",
    "CW_PAIRING_WEB_CLIENT_ID",
    "CW_PAIRING_WEB_REDIRECT_URI",
    "CW_PAIRING_SESSION_SECRET",
}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:access|refresh|runtime|api)[_-]?token\s*[:=]\s*[A-Za-z0-9._~-]{16,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile(r"C:\\Users\\[^\\\s]+\\", re.I),
)


def validation_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    core_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"0\.14\.\d+", core_version) is None:
        errors.append("staging bootstrap Core version must remain on the 0.14.x line")
    for relative in ("Dockerfile", ".dockerignore", "render.yaml"):
        if not (root / relative).is_file():
            errors.append(f"missing deployment artifact: {relative}")
    contract_path = root / "config/staging-environment.json"
    if not contract_path.is_file():
        errors.append("missing staging environment contract")
        return errors
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    variables = contract.get("variables")
    if contract.get("schema_version") != 1 or not isinstance(variables, list):
        errors.append("staging environment contract schema is invalid")
        return errors
    names = {item.get("name") for item in variables if isinstance(item, dict)}
    if not REQUIRED_RENDER_KEYS <= names:
        errors.append("staging environment contract is missing required gateway variables")
    if any(set(item) != {"name", "group", "purpose", "required", "secret", "example", "owner"} for item in variables):
        errors.append("staging environment variable entries must use the exact documented schema")
    if contract.get("gateway_secrets") != ["CW_PAIRING_SESSION_SECRET"]:
        errors.append("browser pairing must require only the provider-managed session cookie secret")
    completion_path = root / "docs/staging-bootstrap-completion-contract.json"
    if not completion_path.is_file():
        errors.append("missing staging bootstrap Completion Contract")
    else:
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        if completion.get("milestone_version") != "0.14.0":
            errors.append("staging bootstrap Completion Contract version is invalid")
        if completion.get("plugin_submission_in_scope") is not False:
            errors.append("staging bootstrap must not authorize plugin submission")
        if completion.get("high_consequence_authorization_in_scope") is not False:
            errors.append("staging bootstrap must not authorize high-consequence capabilities")
    render_path = root / "render.yaml"
    if render_path.is_file():
        render = render_path.read_text(encoding="utf-8")
        render_keys = set(re.findall(r"^\s*- key: ([A-Z0-9_]+)\s*$", render, re.MULTILINE))
        if not REQUIRED_RENDER_KEYS <= render_keys:
            errors.append("render.yaml is missing required environment bindings")
        for phrase in (
            "runtime: docker", "healthCheckPath: /readyz", "mountPath: /var/lib/cw",
            "numInstances: 1", "autoDeployTrigger: checksPass",
            "staging-mcp.cwcli.dev", "sync: false",
        ):
            if phrase not in render:
                errors.append(f"render.yaml is missing required staging setting: {phrase}")
    docker_path = root / "Dockerfile"
    if docker_path.is_file():
        docker = docker_path.read_text(encoding="utf-8")
        for phrase in ("python:3.13-slim@sha256:", "[remote]", "USER cw", "cw.remote.deployment"):
            if phrase not in docker:
                errors.append(f"Dockerfile is missing required boundary: {phrase}")
    for relative in REQUIRED_DOCS:
        if not (root / "docs" / relative).is_file():
            errors.append(f"missing staging documentation: docs/{relative}")
    scanned = [root / "render.yaml", root / "config/staging-environment.json", root / "config/staging-agent.example.env"]
    scanned.extend(root / "docs" / relative for relative in REQUIRED_DOCS)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in scanned if path.is_file())
    for pattern in SECRET_PATTERNS:
        if pattern.search(combined):
            errors.append(f"staging artifacts contain secret/private-path shaped data: {pattern.pattern}")
    if "HIGH_CONSEQUENCE_AUTHORIZATION" not in combined:
        errors.append("staging documentation must retain the high-consequence boundary")
    if "NOT DEPLOYED" not in combined or "NOT EXERCISED" not in combined:
        errors.append("staging evidence must distinguish prepared from executed work")
    return errors


def main() -> int:
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("CW 0.14 Render/Auth0 staging bootstrap contracts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
