from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from cw.application import (
    Actor,
    ActorOrigin,
    ApplicationError,
    ApplicationErrorCode,
    CWApplication,
    OperationContext,
    ResolvedProject,
)
from cw.application.capabilities import CAPABILITIES, CapabilityClass

from .security import sanitize


DiagnosticSink = Callable[[dict[str, Any]], None]
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    project_paths: tuple[Path, ...]
    allowed_roots: tuple[Path, ...]

    @classmethod
    def create(
        cls,
        project_paths: list[Path] | tuple[Path, ...],
        allowed_roots: list[Path] | tuple[Path, ...] | None = None,
    ) -> "RuntimeConfig":
        projects = tuple(Path(item) for item in project_paths)
        if not projects:
            raise ValueError("At least one configured CW project is required")
        roots = tuple(Path(item) for item in (allowed_roots or projects))
        return cls(projects, roots)


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    title: str
    description: str
    capability: str
    application_method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "capability": self.capability,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        }


TOOLS = (
    ToolContract(
        "cw_project_status", "CW project status",
        "Return normalized read-only workflow status for an authorized CW project. "
        "Does not modify project state or read arbitrary repository files.",
        "project.read", "status",
    ),
    ToolContract(
        "cw_project_inspect", "Inspect CW project evidence",
        "Return a normalized read-only project and evidence summary for an authorized CW project. "
        "Does not expose unrestricted files, logs, or local paths.",
        "project.read", "inspect",
    ),
    ToolContract(
        "cw_history", "Inspect CW history",
        "Return the normalized immutable gate/review timeline and CW state events for an authorized "
        "project. Does not modify history.",
        "history.read", "history",
    ),
    ToolContract(
        "cw_explain", "Explain CW workflow state",
        "Explain read-only why an authorized CW project can or cannot advance or complete, using "
        "validated CW evidence. Does not perform recovery or repair.",
        "project.read", "explain",
    ),
    ToolContract(
        "cw_completion_status", "Inspect CW completion status",
        "Return the Completion Contract, latest independent completion review, and extension proposal "
        "for an authorized project. Does not authorize or append work.",
        "completion.read", "completion",
    ),
    ToolContract(
        "cw_gate_status", "Inspect CW phase gates",
        "Return normalized read-only phase-gate states for an authorized CW project. Does not create, "
        "approve, invalidate, or repair gates.",
        "gate.read", "gates",
    ),
)


def _stderr_diagnostic(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


class MCPReadOnlyRuntime:
    """Transport-independent MCP handler with a closed read-only allowlist."""

    def __init__(
        self, config: RuntimeConfig, *, diagnostic_sink: DiagnosticSink | None = None,
    ) -> None:
        self.config = config
        self._diagnostic = diagnostic_sink or _stderr_diagnostic
        self.application = CWApplication(allowed_roots=config.allowed_roots)
        opened = [self.application.open_project(path) for path in config.project_paths]
        self._projects = {item.handle.repository_id: item for item in opened}
        if len(self._projects) != len(opened):
            raise ValueError("Configured CW projects must have unique repository identities")
        self._tools = {item.name: item for item in TOOLS}

    @property
    def private_roots(self) -> tuple[Path, ...]:
        return tuple(item.root for item in self._projects.values())

    def project_handles(self) -> list[dict[str, str]]:
        return [
            project.handle.to_dict()
            for project in sorted(self._projects.values(), key=lambda item: item.handle.repository_id)
        ]

    def tool_contracts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in TOOLS]

    def emit_diagnostic(self, payload: dict[str, Any]) -> None:
        self._diagnostic(payload)

    def _project(self, project_id: str | None) -> ResolvedProject:
        if not project_id:
            if len(self._projects) != 1:
                raise ApplicationError(
                    ApplicationErrorCode.INVALID_REQUEST,
                    "project_id is required when more than one project is configured",
                )
            return next(iter(self._projects.values()))
        return self.application.open_project_handle(project_id)

    @staticmethod
    def _operation_id(value: Any) -> str:
        if value in (None, ""):
            return uuid.uuid4().hex
        if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
            raise ApplicationError(
                ApplicationErrorCode.INVALID_REQUEST,
                "operation_id must be 1-128 safe identifier characters",
            )
        return value

    @staticmethod
    def _error(operation_id: str, error: ApplicationError) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "status": "FAILED",
            "error": {
                "code": error.code.value,
                "message": error.message,
                "retryable": error.retryable,
                "details": error.details,
            },
        }

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        supplied = {} if arguments is None else arguments
        operation_id = uuid.uuid4().hex
        project_id = supplied.get("project_id") if isinstance(supplied, dict) else None
        try:
            if not isinstance(supplied, dict):
                raise ApplicationError(ApplicationErrorCode.INVALID_REQUEST, "Tool arguments must be an object")
            unexpected = set(supplied) - {"project_id", "operation_id"}
            if unexpected:
                raise ApplicationError(
                    ApplicationErrorCode.INVALID_REQUEST,
                    f"Unsupported tool arguments: {', '.join(sorted(unexpected))}",
                )
            if project_id is not None and not isinstance(project_id, str):
                raise ApplicationError(
                    ApplicationErrorCode.INVALID_REQUEST,
                    "project_id must be an opaque string handle",
                )
            operation_id = self._operation_id(supplied.get("operation_id"))
            contract = self._tools.get(name)
            if contract is None:
                raise ApplicationError(
                    ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                    "This read-only MCP runtime does not expose that operation",
                )
            capability = CAPABILITIES.get(contract.capability)
            if (
                capability is None
                or capability.classification is not CapabilityClass.READ
                or capability.mutation
            ):
                raise ApplicationError(
                    ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                    "This MCP runtime exposes read-only capabilities only",
                )
            project = self._project(project_id if isinstance(project_id, str) else None)
            request = OperationContext(
                operation_id,
                Actor("local-mcp-client", ActorOrigin.MCP_CLIENT),
                contract.capability,
            )
            method = getattr(self.application, contract.application_method)
            result = method(project, request=request).to_dict()
            payload = sanitize(result, private_roots=self.private_roots)
            self._diagnostic({
                "event": "tool_invocation", "tool": name,
                "project_id": project.handle.repository_id, "status": "SUCCEEDED",
            })
            return payload
        except ApplicationError as exc:
            payload = sanitize(self._error(operation_id, exc), private_roots=self.private_roots)
            self._diagnostic({
                "event": "structured_error", "tool": name,
                "project_id": project_id if isinstance(project_id, str) else None,
                "code": exc.code.value,
            })
            return payload
        except Exception:
            error = ApplicationError(
                ApplicationErrorCode.INFRASTRUCTURE_FAILURE,
                "CW could not complete the read-only MCP operation",
                retryable=True,
            )
            self._diagnostic({
                "event": "structured_error", "tool": name,
                "project_id": project_id if isinstance(project_id, str) else None,
                "code": error.code.value,
            })
            return self._error(operation_id, error)

    def resource_uris(self) -> list[str]:
        uris = ["cw://projects"]
        suffixes = (
            "summary", "current-phase", "gates", "completion-contract",
            "completion-review/latest", "extension-proposal/current",
        )
        for project_id in sorted(self._projects):
            uris.extend(f"cw://projects/{project_id}/{suffix}" for suffix in suffixes)
        return uris

    def read_resource(self, uri: str) -> dict[str, Any]:
        if uri == "cw://projects":
            return {"schema_version": 1, "projects": self.project_handles()}
        parsed = urlparse(uri)
        parts = [item for item in parsed.path.split("/") if item]
        if parsed.scheme != "cw" or parsed.netloc != "projects" or len(parts) < 2:
            return self._error(uuid.uuid4().hex, ApplicationError(
                ApplicationErrorCode.INVALID_REQUEST, "Unknown CW resource",
            ))
        project_id, kind = parts[0], "/".join(parts[1:])
        tool_by_kind = {
            "summary": "cw_project_status",
            "gates": "cw_gate_status",
            "completion-contract": "cw_completion_status",
            "completion-review/latest": "cw_completion_status",
            "extension-proposal/current": "cw_completion_status",
        }
        if kind == "current-phase":
            result = self.call_tool("cw_project_status", {"project_id": project_id})
            if result.get("status") == "FAILED":
                return result
            data = result.get("data", {})
            resource_data = {
                "phase": data.get("phase"), "position": data.get("position"),
                "phase_count": data.get("phase_count"), "state": data.get("state"),
            }
        elif kind in tool_by_kind:
            result = self.call_tool(tool_by_kind[kind], {"project_id": project_id})
            if result.get("status") == "FAILED":
                return result
            source = result.get("data", {})
            if kind == "completion-contract":
                resource_data = source.get("contract")
            elif kind == "completion-review/latest":
                resource_data = source.get("review")
            elif kind == "extension-proposal/current":
                resource_data = source.get("proposal")
            else:
                resource_data = source
        else:
            return self._error(uuid.uuid4().hex, ApplicationError(
                ApplicationErrorCode.INVALID_REQUEST, "Unknown CW resource",
            ))
        return sanitize({
            "schema_version": 1,
            "project_id": project_id,
            "resource": kind,
            "data": resource_data,
        }, private_roots=self.private_roots)
