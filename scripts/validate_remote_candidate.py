#!/usr/bin/env python3
"""Validate static CW 0.13 remote-candidate contracts without network access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCS = (
    "remote-gateway.md", "remote-agent.md", "remote-auth.md", "remote-pairing.md",
    "remote-project-grants.md", "remote-security.md", "remote-privacy.md",
    "remote-operations.md", "acceptance/remote-gateway-0.13.md",
    "adr/0006-gateway-agent-long-poll.md", "adr/0007-remote-identity-provider.md",
    "adr/0008-device-identity-pairing.md", "adr/0009-remote-persistence.md",
)
EXPECTED_TOOLS = {
    "cw_project_status", "cw_project_inspect", "cw_history", "cw_explain",
    "cw_completion_status", "cw_gate_status", "cw_phase_start", "cw_validate",
    "cw_request_review", "cw_retry", "cw_operation_status", "cw_operation_cancel",
}
FORBIDDEN = {
    "cw_execute", "shell", "filesystem_read", "git", "cw_create_gate",
    "cw_approve_gate", "cw_authorize_extension", "cw_repair", "cw_rebaseline",
    "cw_release", "cw_deploy",
}


def validation_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    contract_path = root / "docs/remote-gateway-completion-contract.json"
    try:
        version_parts = tuple(int(item) for item in version.split("."))
    except ValueError:
        version_parts = ()
    if version_parts < (0, 13, 0):
        errors.append("current VERSION must retain the 0.13 remote candidate or a later compatible milestone")
    if not contract_path.is_file():
        errors.append("missing remote Completion Contract")
        return errors
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("milestone_version") != "0.13.0":
        errors.append("remote Completion Contract must remain historical 0.13 evidence")
    if contract.get("public_deployment_in_scope") is not False or contract.get("plugin_submission_in_scope") is not False:
        errors.append("0.13 must not authorize deployment or plugin submission")
    for relative in REQUIRED_DOCS:
        if not (root / "docs" / relative).is_file():
            errors.append(f"missing remote artifact: docs/{relative}")
    try:
        sys.path.insert(0, str(root))
        from cw.remote.protocol import PROTOCOL_VERSION, REMOTE_TOOLS
        if PROTOCOL_VERSION != "cw.remote.v1":
            errors.append("remote protocol version drifted")
        if set(REMOTE_TOOLS) != EXPECTED_TOOLS or FORBIDDEN & set(REMOTE_TOOLS):
            errors.append("remote tool registry is not the accepted closed surface")
    except Exception as exc:  # pragma: no cover - printed for direct validation
        errors.append(f"cannot validate remote registry: {exc}")
    docs = "\n".join(
        (root / "docs" / relative).read_text(encoding="utf-8")
        for relative in REQUIRED_DOCS if (root / "docs" / relative).is_file()
    )
    for phrase in (
        "2026-08-15", "cw.remote.v1", "OAuth 2.1", "PKCE", "CIMD", "DCR",
        "outbound-only", "HIGH_CONSEQUENCE", "NOT RUN",
    ):
        if phrase not in docs:
            errors.append(f"remote documentation is missing required fact: {phrase}")
    if re.search(r"/home/[^\s`]+|C:\\Users\\|sk-[A-Za-z0-9]", docs):
        errors.append("remote artifacts contain a private path or secret-shaped value")
    return errors


def main() -> int:
    errors = validation_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("CW 0.13 remote gateway candidate contracts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
