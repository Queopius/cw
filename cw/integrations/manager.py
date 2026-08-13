from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from cw.core.errors import CwError, ErrorCode
from cw.core.utils import atomic_json, load_json, utc_now
from cw.update.config import config_dir

from .diagnostics import parse_mcp_diagnostics
from .models import Integration, IntegrationHealth, Requirement


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class IntegrationCheck:
    integrations: tuple[Integration, ...]
    stderr: str = ""
    exit_code: int = 0


class IntegrationManager:
    def __init__(self, command: str = "codex", *, runner: Runner = subprocess.run, cache_path: Path | None = None) -> None:
        self.command = command
        self.runner = runner
        self.cache_path = cache_path or config_dir() / "integrations.json"

    def configured(
        self, required: set[str] | None = None, *, disable_plugins: bool = False,
    ) -> tuple[Integration, ...]:
        required = required or set()
        if shutil.which(self.command) is None and self.command == "codex":
            raise CwError("Codex CLI was not found", ErrorCode.CODEX_NOT_FOUND)
        command = [self.command, "mcp"]
        if disable_plugins:
            command.extend(["--disable", "plugins"])
        command.append("list")
        completed = self.runner(
            command, text=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=20, check=False,
        )
        if completed.returncode:
            raise CwError("Could not inspect Codex MCP configuration", ErrorCode.MCP_TRANSPORT_ERROR, details=completed.stderr[-3000:])
        integrations: list[Integration] = []
        for raw in completed.stdout.splitlines()[1:]:
            columns = raw.split()
            if len(columns) < 2 or columns[0].lower() in {"name", "no"}:
                continue
            name = columns[0].lower()
            enabled = "disabled" not in raw.lower()
            health = IntegrationHealth.UNKNOWN if enabled else IntegrationHealth.DISABLED
            requirement = Requirement.REQUIRED if name in required else Requirement.OPTIONAL
            integrations.append(Integration(name, "mcp", enabled, requirement, health))
        known = {item.id for item in integrations}
        for name in sorted(required - known):
            integrations.append(Integration(
                name, "mcp", False, Requirement.REQUIRED, IntegrationHealth.UNKNOWN,
                error_code=ErrorCode.MCP_NOT_CONFIGURED.value,
            ))
        return tuple(integrations)

    def check(self, root: Path, *, required: set[str] | None = None, force: bool = False) -> IntegrationCheck:
        required = required or set()
        if not force:
            cached = self._cached(required)
            if cached is not None:
                return cached
        configured = self.configured(required)
        command = [
            self.command, "--strict-config", "--disable", "hooks", "--cd", str(root),
            "--sandbox", "read-only", "--ask-for-approval", "never", "exec",
            "--ephemeral", "--ignore-rules", "--color", "never",
            "Integration health check only. Do not inspect project files. Reply exactly: INTEGRATIONS_OK",
        ]
        completed = self.runner(
            command, cwd=root, env=os.environ.copy(), text=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=90, check=False,
        )
        diagnostics = {item.integration: item for item in parse_mcp_diagnostics(completed.stderr)}
        resolved: list[Integration] = []
        for item in configured:
            diagnostic = diagnostics.get(item.id)
            if item.error_code == ErrorCode.MCP_NOT_CONFIGURED.value:
                resolved.append(item)
            elif not item.enabled:
                health = IntegrationHealth.DISABLED
                code = item.error_code or ErrorCode.MCP_DISABLED.value
                resolved.append(Integration(
                    item.id, item.type, False, item.required, health,
                    error_code=code,
                ))
            elif diagnostic:
                resolved.append(Integration(
                    item.id, item.type, True, item.required, diagnostic.status,
                    error_code=diagnostic.error_code, http_status=diagnostic.http_status,
                    occurrences=diagnostic.occurrences,
                ))
            else:
                health = IntegrationHealth.AVAILABLE if completed.returncode == 0 else IntegrationHealth.UNKNOWN
                resolved.append(Integration(item.id, item.type, item.enabled, item.required, health))
        result = IntegrationCheck(tuple(resolved), completed.stderr, completed.returncode)
        self._store(result)
        return result

    def preflight(self, root: Path, required: set[str]) -> tuple[Integration, ...]:
        if not required:
            return ()
        result = self.check(root, required=required, force=True)
        for item in result.integrations:
            if item.required is not Requirement.REQUIRED or item.health is IntegrationHealth.AVAILABLE:
                continue
            if item.error_code == ErrorCode.MCP_NOT_CONFIGURED.value:
                code = ErrorCode.MCP_NOT_CONFIGURED
                message = f"Required integration is not configured: {item.id}"
            elif item.health is IntegrationHealth.DISABLED:
                code = ErrorCode.MCP_DISABLED
                message = f"Required integration is disabled: {item.id}"
            elif item.health is IntegrationHealth.AUTH_REQUIRED:
                code = ErrorCode.MCP_AUTH_REQUIRED
                message = f"Required integration needs authentication: {item.id}"
            else:
                code = ErrorCode.MCP_REQUIRED_UNAVAILABLE
                message = f"Required integration is unavailable: {item.id}"
            detail = f"Integration: {item.id}\nStatus: {item.health.value}"
            if item.http_status:
                detail += f"\nHTTP: {item.http_status}"
            raise CwError(message, code, "Run: cw integrations check", details=detail, exit_code=3)
        return result.integrations

    def _cached(self, required: set[str]) -> IntegrationCheck | None:
        if not self.cache_path.is_file():
            return None
        try:
            value = load_json(self.cache_path)
            checked = datetime.fromisoformat(str(value["checked_at"]).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - checked > timedelta(minutes=5):
                return None
            items = tuple(_integration_from_dict(item, required) for item in value["integrations"])
            return IntegrationCheck(items, "", int(value.get("exit_code", 0)))
        except Exception:
            return None

    def _store(self, result: IntegrationCheck) -> None:
        atomic_json(self.cache_path, {
            "schema_version": 1, "checked_at": utc_now(), "exit_code": result.exit_code,
            "integrations": [item.to_dict() for item in result.integrations],
        })


def _integration_from_dict(value: dict, required: set[str]) -> Integration:
    name = str(value["id"])
    return Integration(
        name, str(value.get("type", "mcp")), bool(value.get("enabled", True)),
        Requirement.REQUIRED if name in required else Requirement.OPTIONAL,
        IntegrationHealth(str(value.get("health", "UNKNOWN"))),
        error_code=value.get("error_code"), http_status=value.get("http_status"),
        occurrences=int(value.get("occurrences", 0)),
    )
