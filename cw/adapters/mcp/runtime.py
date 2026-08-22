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
    OperationStatus,
    ResolvedProject,
)
from cw.application.capabilities import CAPABILITIES, CapabilityClass

from .security import sanitize


DiagnosticSink = Callable[[dict[str, Any]], None]
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROJECT_ID = re.compile(r"[0-9a-f]{20}")
_OUTPUT_FIELDS = {
    "schema_version", "operation_id", "operation", "capability", "project_id",
    "status", "idempotent_replay", "actor_origin", "data", "error",
}


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    project_paths: tuple[Path, ...]
    allowed_roots: tuple[Path, ...]
    actor: Actor
    enabled_tools: frozenset[str] | None = None
    surface: str = "local-stdio"

    @classmethod
    def create(
        cls,
        project_paths: list[Path] | tuple[Path, ...],
        allowed_roots: list[Path] | tuple[Path, ...] | None = None,
        *,
        actor: Actor | None = None,
        enabled_tools: frozenset[str] | set[str] | None = None,
        surface: str = "local-stdio",
    ) -> "RuntimeConfig":
        projects = tuple(Path(item) for item in project_paths)
        if not projects:
            raise ValueError("At least one configured CW project is required")
        roots = tuple(Path(item) for item in (allowed_roots or projects))
        return cls(
            projects,
            roots,
            actor or Actor("local-mcp-client", ActorOrigin.MCP_CLIENT),
            None if enabled_tools is None else frozenset(enabled_tools),
            surface,
        )


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    title: str
    description: str
    capability: str
    application_method: str
    mutation: bool = False
    long_running: bool = False
    allowed_arguments: tuple[str, ...] = ("project_id", "operation_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "capability": self.capability,
            "annotations": {
                "readOnlyHint": not self.mutation,
                "destructiveHint": False,
                "idempotentHint": not self.mutation,
                "openWorldHint": False,
            },
            "mutation": self.mutation,
            "long_running": self.long_running,
        }

    def input_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        if "project_id" in self.allowed_arguments:
            properties["project_id"] = {
                "type": "string", "maxLength": 256,
                "description": "Opaque authorized CW project handle; omit only for a single-project runtime.",
            }
        if "operation_id" in self.allowed_arguments:
            properties["operation_id"] = {
                "type": "string", "pattern": "^(?:|[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$",
                "description": "Stable caller operation identifier; reuse it to make retries replay-safe.",
            }
        if "target_operation_id" in self.allowed_arguments:
            properties["target_operation_id"] = {
                "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "additionalProperties": False,
            "properties": properties,
            "required": ["target_operation_id"] if "target_operation_id" in properties else [],
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "additionalProperties": False,
            "required": ["schema_version", "operation_id", "status"],
            "properties": {
                "schema_version": {"const": 1},
                "operation_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
                "operation": {"type": "string"},
                "capability": {"type": "string", "minLength": 1},
                "project_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "status": {"enum": ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"]},
                "idempotent_replay": {"type": "boolean"},
                "actor_origin": {"type": ["string", "null"]},
                "data": {"type": "object"},
                "error": {
                    "type": "object", "additionalProperties": False,
                    "required": ["code", "message", "retryable", "details"],
                    "properties": {
                        "code": {"type": "string"}, "message": {"type": "string"},
                        "retryable": {"type": "boolean"}, "details": {"type": "object"},
                    },
                },
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
    ToolContract(
        "cw_phase_start", "Start the authorized CW phase",
        "Start only the current engine-authorized phase and create its implementation session. "
        "Mutates phase/session metadata, never selects an arbitrary phase, creates a gate, or invokes Codex. "
        "Returns an operation ID; duplicate invocation with that ID is safe.",
        "phase.start", "phase_start", mutation=True, long_running=True,
    ),
    ToolContract(
        "cw_validate", "Validate the current CW phase",
        "Run only the current phase's configured deterministic validation contract. Mutates only "
        "validation/operation evidence, never accepts a command, and may take time. Returns an operation ID.",
        "validation.run", "validate", mutation=True, long_running=True,
    ),
    ToolContract(
        "cw_request_review", "Request independent CW review",
        "Request the existing independent read-only reviewer for the current authorized phase. May invoke "
        "Codex and take time; only supervisor checks may create a gate. The caller cannot supply a decision.",
        "review.run", "request_review", mutation=True, long_running=True,
    ),
    ToolContract(
        "cw_retry", "Retry a controlled CW operation",
        "Retry only the current engine-classified retryable implementation or review operation. Does not "
        "rewind history, remove gates, reopen completion, or authorize an extension. A review retry may invoke "
        "the independent Codex reviewer and take time. Returns an operation ID.",
        "retry.run", "retry", mutation=True, long_running=True,
    ),
    ToolContract(
        "cw_operation_status", "Inspect a CW operation",
        "Poll normalized lifecycle and sanitized result data for one operation in the authorized project. "
        "Does not alter workflow evidence or permit cross-project operation access.",
        "operation.read", "operation_status",
        allowed_arguments=("project_id", "operation_id", "target_operation_id"),
    ),
    ToolContract(
        "cw_operation_cancel", "Cancel a queued CW operation",
        "Cancel a controlled operation only before execution begins. Never fabricates phase, validation, "
        "review, or gate outcomes; an already running mutation is refused as unsafe to cancel.",
        "operation.cancel", "cancel_operation", mutation=True,
        allowed_arguments=("project_id", "operation_id", "target_operation_id"),
    ),
)


def _stderr_diagnostic(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


class MCPRuntime:
    """Transport-independent MCP handler with a closed governed allowlist.

    The historical class name remains an import-compatible alias for 0.8
    consumers; its registered 0.9 surface is defined only by ``TOOLS``.
    """

    def __init__(
        self, config: RuntimeConfig, *, diagnostic_sink: DiagnosticSink | None = None,
        review_backend_factory: object | None = None,
        operation_workers: int = 2,
    ) -> None:
        self.config = config
        self._diagnostic = diagnostic_sink or _stderr_diagnostic
        self.application = CWApplication(
            allowed_roots=config.allowed_roots,
            review_backend_factory=review_backend_factory,
            operation_workers=operation_workers,
        )
        opened = [self.application.open_project(path) for path in config.project_paths]
        self._projects = {item.handle.repository_id: item for item in opened}
        if len(self._projects) != len(opened):
            raise ValueError("Configured CW projects must have unique repository identities")
        self._tools = {item.name: item for item in TOOLS}
        configured_tools = set(self._tools) if config.enabled_tools is None else set(config.enabled_tools)
        unknown_tools = configured_tools - set(self._tools)
        if unknown_tools:
            raise ValueError(
                "Configured MCP surface contains unknown tools: " + ", ".join(sorted(unknown_tools))
            )
        self._enabled_tools = frozenset(configured_tools)

    @property
    def private_roots(self) -> tuple[Path, ...]:
        return tuple(item.root for item in self._projects.values())

    def project_handles(self) -> list[dict[str, str]]:
        return [
            project.handle.to_dict()
            for project in sorted(self._projects.values(), key=lambda item: item.handle.repository_id)
        ]

    def tool_contracts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in TOOLS if item.name in self._enabled_tools]

    def tool_contract(self, name: str) -> ToolContract:
        contract = self._tools.get(name)
        if contract is None or name not in self._enabled_tools:
            raise KeyError(name)
        return contract

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
        project = self._projects.get(project_id)
        if project is None:
            raise ApplicationError(
                ApplicationErrorCode.PROJECT_SCOPE_VIOLATION,
                "Project handle is not authorized for this MCP runtime",
            )
        return project

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

    @staticmethod
    def _validate_output(contract: ToolContract, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) - _OUTPUT_FIELDS:
            raise ApplicationError(
                ApplicationErrorCode.STATE_INCONSISTENT,
                "CW produced an invalid MCP output contract",
            )
        if value.get("schema_version") != 1 or not isinstance(value.get("operation_id"), str):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if _OPERATION_ID.fullmatch(value["operation_id"]) is None:
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if value.get("status") not in {item.value for item in OperationStatus}:
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "operation" in value and not isinstance(value["operation"], str):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "capability" in value and not isinstance(value["capability"], str):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if (
            "capability" in value
            and contract.name not in {"cw_operation_status", "cw_operation_cancel"}
            and value["capability"] != contract.capability
        ):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "project_id" in value and (
            not isinstance(value["project_id"], str) or _PROJECT_ID.fullmatch(value["project_id"]) is None
        ):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "idempotent_replay" in value and not isinstance(value["idempotent_replay"], bool):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "actor_origin" in value and value["actor_origin"] is not None and not isinstance(value["actor_origin"], str):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "data" in value and not isinstance(value["data"], dict):
            raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        if "error" in value:
            error = value["error"]
            if (
                not isinstance(error, dict)
                or set(error) != {"code", "message", "retryable", "details"}
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
                or not isinstance(error.get("retryable"), bool)
                or not isinstance(error.get("details"), dict)
            ):
                raise ApplicationError(ApplicationErrorCode.STATE_INCONSISTENT, "CW produced an invalid MCP output contract")
        return value

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        supplied = {} if arguments is None else arguments
        operation_id = uuid.uuid4().hex
        project_id = supplied.get("project_id") if isinstance(supplied, dict) else None
        try:
            if not isinstance(supplied, dict):
                raise ApplicationError(ApplicationErrorCode.INVALID_REQUEST, "Tool arguments must be an object")
            contract = self._tools.get(name)
            if contract is None:
                raise ApplicationError(
                    ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                    "This MCP runtime does not expose that operation",
                )
            if name not in self._enabled_tools:
                raise ApplicationError(
                    ApplicationErrorCode.PLATFORM_CAPABILITY_UNAVAILABLE,
                    "CW supports this capability, but it is unavailable on the configured client surface",
                    details={
                        "cw_capability_supported": True,
                        "surface": self.config.surface,
                        "surface_capability_available": False,
                    },
                )
            unexpected = set(supplied) - set(contract.allowed_arguments)
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
            capability = CAPABILITIES.get(contract.capability)
            allowed_classes = {
                CapabilityClass.READ,
                CapabilityClass.EXECUTION,
                CapabilityClass.CONTROLLED_STATE_MUTATION,
            }
            if capability is None or capability.classification not in allowed_classes:
                raise ApplicationError(
                    ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                    "This MCP runtime does not expose that capability class",
                )
            if capability.human_authorization_required:
                raise ApplicationError(
                    ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                    "High-consequence authorization is unavailable over MCP",
                )
            if capability.mutation != contract.mutation:
                raise ApplicationError(
                    ApplicationErrorCode.STATE_INCONSISTENT,
                    "MCP tool mutation annotation does not match application policy",
                )
            project = self._project(project_id if isinstance(project_id, str) else None)
            request = OperationContext(
                operation_id,
                self.config.actor,
                contract.capability,
            )
            method = getattr(self.application, contract.application_method)
            if "target_operation_id" in contract.allowed_arguments:
                target = supplied.get("target_operation_id")
                if not isinstance(target, str) or _OPERATION_ID.fullmatch(target) is None:
                    raise ApplicationError(
                        ApplicationErrorCode.INVALID_REQUEST,
                        "target_operation_id must identify an existing operation",
                    )
                try:
                    result = method(
                        project, target_operation_id=target, request=request,
                    ).to_dict()
                except ApplicationError as exc:
                    if exc.code is ApplicationErrorCode.OPERATION_NOT_FOUND:
                        token = __import__("hashlib").sha256(target.encode("utf-8")).hexdigest()
                        if any(
                            other.handle.repository_id != project.handle.repository_id
                            and (other.root / ".cw" / "runtime" / "operations" / f"{token}.json").is_file()
                            for other in self._projects.values()
                        ):
                            raise ApplicationError(
                                ApplicationErrorCode.PROJECT_SCOPE_VIOLATION,
                                "The operation belongs to another authorized project",
                            ) from exc
                    raise
            else:
                result = method(project, request=request).to_dict()
            result = self._validate_output(contract, result)
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
                "CW could not complete the MCP operation",
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

    def shutdown(self, *, wait: bool = True) -> None:
        self.application.shutdown(wait=wait)


# Keep the 0.8 name as a source-compatible alias for existing integrations.
MCPReadOnlyRuntime = MCPRuntime
