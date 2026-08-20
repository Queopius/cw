from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from cw.adapters.mcp.runtime import TOOLS, ToolContract
from cw.application.capabilities import CAPABILITIES, CapabilityClass

from .errors import RemoteError, RemoteErrorCode


PROTOCOL_VERSION = "cw.remote.v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_HANDLE = re.compile(r"cwp_[A-Za-z0-9_-]{24,96}")
_TOOL_MAP = {tool.name: tool for tool in TOOLS}

CAPABILITY_SCOPES: Mapping[str, str] = {
    "project.read": "project.read",
    "gate.read": "gate.read",
    "history.read": "history.read",
    "completion.read": "completion.read",
    "operation.read": "operation.read",
    "validation.run": "validation.execute",
    "review.run": "review.execute",
    "phase.start": "phase.start",
    "retry.run": "retry.execute",
    "operation.cancel": "operation.cancel",
}

REMOTE_READ_TOOLS = frozenset({
    "cw_project_status",
    "cw_project_inspect",
    "cw_history",
    "cw_explain",
    "cw_completion_status",
    "cw_gate_status",
})
REMOTE_CONTROLLED_TOOLS = frozenset(
    tool.name for tool in TOOLS
    if tool.name not in REMOTE_READ_TOOLS and CAPABILITIES[tool.capability].classification
    in {CapabilityClass.READ, CapabilityClass.EXECUTION, CapabilityClass.CONTROLLED_STATE_MUTATION}
)
REMOTE_TOOLS = REMOTE_READ_TOOLS | REMOTE_CONTROLLED_TOOLS


def tool_contract(name: str) -> ToolContract:
    contract = _TOOL_MAP.get(name)
    if contract is None or name not in REMOTE_TOOLS:
        raise RemoteError(
            RemoteErrorCode.AUTHORIZATION_REQUIRED,
            "The remote CW surface does not expose that operation",
            http_status=403,
        )
    capability = CAPABILITIES.get(contract.capability)
    if capability is None or capability.human_authorization_required:
        raise RemoteError(
            RemoteErrorCode.AUTHORIZATION_REQUIRED,
            "High-consequence authorization is not available remotely",
            http_status=403,
        )
    if capability.classification not in {
        CapabilityClass.READ,
        CapabilityClass.EXECUTION,
        CapabilityClass.CONTROLLED_STATE_MUTATION,
    }:
        raise RemoteError(
            RemoteErrorCode.AUTHORIZATION_REQUIRED,
            "The remote CW surface does not expose that capability class",
            http_status=403,
        )
    return contract


def required_scope(name: str) -> str:
    contract = tool_contract(name)
    try:
        return CAPABILITY_SCOPES[contract.capability]
    except KeyError as exc:  # pragma: no cover - registry drift defense
        raise RemoteError(
            RemoteErrorCode.INTERNAL_ERROR,
            "The remote scope registry is inconsistent",
            http_status=500,
        ) from exc


def all_remote_scopes() -> tuple[str, ...]:
    return tuple(sorted({required_scope(name) for name in REMOTE_TOOLS}))


def canonical_digest(tool: str, arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"tool": tool, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    principal_id: str
    workspace_id: str
    client_id: str
    scopes: frozenset[str]
    token_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("principal", self.principal_id),
            ("workspace", self.workspace_id),
            ("client", self.client_id),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise RemoteError(RemoteErrorCode.TOKEN_INVALID, f"OAuth {label} identity is invalid", http_status=401)


@dataclass(frozen=True, slots=True)
class RemoteRequest:
    request_id: str
    operation_id: str
    principal_id: str
    workspace_id: str
    device_id: str
    project_handle: str
    actor: str
    origin: str
    capability: str
    tool: str
    arguments: dict[str, Any]
    arguments_digest: str
    created_at: str
    deadline_at: str
    protocol_version: str = PROTOCOL_VERSION

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        operation_id: str,
        identity: RemoteIdentity,
        device_id: str,
        project_handle: str,
        tool: str,
        arguments: Mapping[str, Any],
        created_at: str,
        deadline_at: str,
    ) -> "RemoteRequest":
        contract = tool_contract(tool)
        supplied = dict(arguments)
        allowed = set(contract.allowed_arguments)
        if set(supplied) - allowed:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote tool arguments do not match the closed schema")
        # The remote handle is routing state.  The local adapter substitutes its
        # startup-authorized project identity before invoking MCPRuntime.
        supplied.pop("project_id", None)
        supplied["operation_id"] = operation_id
        request = cls(
            request_id=request_id,
            operation_id=operation_id,
            principal_id=identity.principal_id,
            workspace_id=identity.workspace_id,
            device_id=device_id,
            project_handle=project_handle,
            actor=identity.principal_id,
            origin="remote_plugin",
            capability=contract.capability,
            tool=tool,
            arguments=supplied,
            arguments_digest=canonical_digest(tool, supplied),
            created_at=created_at,
            deadline_at=deadline_at,
        )
        request.validate()
        return request

    def validate(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise RemoteError(
                RemoteErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                "The gateway and agent protocol versions are incompatible",
            )
        for value in (
            self.request_id,
            self.operation_id,
            self.principal_id,
            self.workspace_id,
            self.device_id,
            self.actor,
        ):
            if _SAFE_ID.fullmatch(value) is None:
                raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote request identity is invalid")
        if self.actor != self.principal_id:
            raise RemoteError(
                RemoteErrorCode.AUTHORIZATION_REQUIRED,
                "Remote actor cannot differ from the authenticated principal",
            )
        if _HANDLE.fullmatch(self.project_handle) is None:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Project handle is malformed")
        contract = tool_contract(self.tool)
        if contract.capability != self.capability:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Tool capability does not match the registry")
        if self.origin != "remote_plugin":
            raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Remote actor origin cannot be caller-selected")
        if canonical_digest(self.tool, self.arguments) != self.arguments_digest:
            raise RemoteError(RemoteErrorCode.OPERATION_CONFLICT, "Remote request digest does not match its payload")
        if set(self.arguments) - set(contract.allowed_arguments):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote request contains unsupported arguments")
        _timestamp(self.created_at)
        if _timestamp(self.deadline_at) <= _timestamp(self.created_at):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote request deadline is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "principal_id": self.principal_id,
            "workspace_id": self.workspace_id,
            "device_id": self.device_id,
            "project_handle": self.project_handle,
            "actor": self.actor,
            "origin": self.origin,
            "capability": self.capability,
            "tool": self.tool,
            "arguments": self.arguments,
            "arguments_digest": self.arguments_digest,
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RemoteRequest":
        expected = {
            "protocol_version", "request_id", "operation_id", "principal_id",
            "workspace_id", "device_id", "project_handle", "actor", "origin",
            "capability", "tool", "arguments", "arguments_digest", "created_at",
            "deadline_at",
        }
        if set(payload) != expected or not isinstance(payload.get("arguments"), dict):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote request schema is invalid")
        request = cls(**dict(payload))
        request.validate()
        return request


@dataclass(frozen=True, slots=True)
class RemoteResponse:
    request_id: str
    operation_id: str
    project_handle: str
    status: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    protocol_version: str = PROTOCOL_VERSION

    def validate(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise RemoteError(RemoteErrorCode.PROTOCOL_VERSION_UNSUPPORTED, "Remote response version is unsupported")
        if _SAFE_ID.fullmatch(self.request_id) is None or _SAFE_ID.fullmatch(self.operation_id) is None:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote response identity is invalid")
        if _HANDLE.fullmatch(self.project_handle) is None:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote response project handle is invalid")
        if self.status not in {"SUCCEEDED", "FAILED", "BLOCKED", "QUEUED", "RUNNING", "CANCELLED"}:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote response status is invalid")
        if (self.result is None) == (self.error is None):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote response must contain exactly one result or error")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "project_handle": self.project_handle,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RemoteResponse":
        expected = {
            "protocol_version", "request_id", "operation_id", "project_handle",
            "status", "result", "error",
        }
        if set(payload) != expected:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote response schema is invalid")
        response = cls(**dict(payload))
        response.validate()
        return response
