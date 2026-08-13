from __future__ import annotations

import re

from .models import IntegrationDiagnostic, IntegrationHealth


_START = re.compile(r"MCP client for [`'](?P<name>[A-Za-z0-9_-]+)[`'] failed to start", re.IGNORECASE)
_FAILED = re.compile(r"MCP startup incomplete \(failed:\s*(?P<names>[^)]+)\)", re.IGNORECASE)
_HTTP = re.compile(r"HTTP\s+(?P<status>[45]\d\d)", re.IGNORECASE)


def parse_mcp_diagnostics(stderr: str) -> tuple[IntegrationDiagnostic, ...]:
    matches = list(_START.finditer(stderr))
    if not matches:
        names: list[str] = []
        for match in _FAILED.finditer(stderr):
            names.extend(value.strip().lower() for value in match.group("names").split(",") if value.strip())
        return tuple(
            IntegrationDiagnostic(name, IntegrationHealth.UNAVAILABLE, "MCP_TRANSPORT_ERROR", summary="MCP transport could not initialize")
            for name in dict.fromkeys(names)
        )
    grouped: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(stderr), match.start() + 6000)
        grouped.setdefault(match.group("name").lower(), []).append(stderr[match.start():end])
    if not grouped:
        return ()
    diagnostics: list[IntegrationDiagnostic] = []
    for name, windows in grouped.items():
        text = "\n".join(windows).lower()
        http = _HTTP.search("\n".join(windows))
        status = int(http.group("status")) if http else None
        if any(term in text for term in ("invalid_token", "authrequired", "authorization required", "authentication required")):
            health, code, summary = IntegrationHealth.AUTH_REQUIRED, "MCP_AUTH_REQUIRED", "Authentication is required"
        elif status is not None and status >= 500:
            health, code, summary = IntegrationHealth.UNAVAILABLE, "MCP_SERVER_ERROR", f"Server returned HTTP {status}"
        else:
            health, code, summary = IntegrationHealth.UNAVAILABLE, "MCP_TRANSPORT_ERROR", "MCP transport could not initialize"
        diagnostics.append(IntegrationDiagnostic(name, health, code, status, len(windows), summary))
    return tuple(diagnostics)
