from __future__ import annotations

import os
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from cw import __version__
from cw.adapters.mcp.runtime import TOOLS
from cw.core.utils import utc_now

from .auth import (
    OAuthResourceConfig,
    OAuthResourceMiddleware,
    current_identity,
    protected_resource_metadata,
)
from .device import verify_device_signature
from .errors import RemoteError, RemoteErrorCode
from .gateway import GatewayService
from .protocol import PROTOCOL_VERSION, REMOTE_TOOLS, RemoteResponse, required_scope


SEMVER_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def _read_plugin_version(environment: Mapping[str, str] | None = None) -> str:
    if environment is not None:
        explicit = environment.get("CW_PLUGIN_VERSION", "").strip()
    else:
        explicit = os.getenv("CW_PLUGIN_VERSION", "").strip()
    if explicit:
        return explicit

    candidate_roots = [*Path(__file__).resolve().parents, Path.cwd(), Path("/app")]
    for root in candidate_roots:
        plugin_version_file = root / "plugins" / "cw" / "VERSION"
        try:
            value = plugin_version_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "0.0.0-unknown"


def _dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - optional boundary
        raise RuntimeError("The remote gateway requires codex-workflow[remote]") from exc
    return FastMCP, ToolAnnotations, Request, JSONResponse


def _validate_plugin_version(value: str) -> None:
    if not SEMVER_PATTERN.fullmatch(value):
        raise ValueError("Gateway plugin version must be SemVer")


def _remote_failure(error: RemoteError, operation_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation_id": operation_id or uuid.uuid4().hex,
        "status": "FAILED",
        "error": error.to_dict(),
    }


@dataclass(frozen=True, slots=True)
class GatewayRuntimeIdentity:
    """Sanitized deploy identity exposed by health/readiness diagnostics."""

    environment: str = "development"
    build_sha: str = "development"
    cw_core_version: str = __version__
    cw_plugin_version: str = _read_plugin_version()
    remote_protocol_version: str = PROTOCOL_VERSION
    # Compatibility aliases retained for existing callers.
    version: str = __version__
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.environment) is None:
            raise ValueError("Gateway deployment environment is invalid")
        if self.build_sha != "development" and re.fullmatch(r"[0-9a-f]{40}", self.build_sha) is None:
            raise ValueError("Gateway build SHA must be a full lowercase Git SHA")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", self.cw_core_version):
            raise ValueError("Gateway core version must be SemVer")
        _validate_plugin_version(self.cw_plugin_version)

    def to_dict(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "cw_core_version": self.cw_core_version,
            "cw_plugin_version": self.cw_plugin_version,
            "remote_protocol_version": self.remote_protocol_version,
            "version": self.version,
            "build_sha": self.build_sha,
            "protocol_version": self.protocol_version,
        }


def create_gateway_app(
    service: GatewayService,
    oauth: OAuthResourceConfig,
    *,
    runtime_identity: GatewayRuntimeIdentity | None = None,
    allowed_hosts: tuple[str, ...] = (),
) -> Any:
    """Create a hosting-neutral ASGI application with Streamable HTTP at /mcp."""

    FastMCP, ToolAnnotations, _, JSONResponse = _dependencies()
    from mcp.server.transport_security import TransportSecuritySettings
    resource_host = urlparse(oauth.resource).netloc
    identity = runtime_identity or GatewayRuntimeIdentity()
    trusted_hosts = list(dict.fromkeys([
        resource_host,
        *allowed_hosts,
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
    ]))
    server = FastMCP(
        "CW — Codex Workflow Remote Gateway",
        instructions=(
            "Use CW evidence as authoritative. No valid gate, no next phase. "
            "Conversation is not high-consequence authorization. The gateway exposes "
            "only the closed CW read and controlled-action registry."
        ),
        log_level="WARNING",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=service.router.limits.maximum_request_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=trusted_hosts,
            allowed_origins=[],
        ),
    )

    contracts = {contract.name: contract for contract in TOOLS if contract.name in REMOTE_TOOLS}

    async def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation_id = arguments.get("operation_id") or uuid.uuid4().hex
        project_id = arguments.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return _remote_failure(RemoteError(
                RemoteErrorCode.PROJECT_NOT_GRANTED,
                "An opaque authorized project handle is required",
                http_status=403,
            ), operation_id)
        try:
            return await service.router.dispatch(
                current_identity(),
                project_handle=project_id,
                tool=name,
                arguments=arguments,
                request_id=operation_id,
                operation_id=operation_id,
            )
        except RemoteError as exc:
            return _remote_failure(exc, operation_id)

    def register(name: str, function: Callable[..., Any]) -> None:
        contract = contracts[name]
        server.tool(
            name=name,
            title=contract.title,
            description=contract.description,
            annotations=ToolAnnotations(
                readOnlyHint=not contract.mutation,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
            meta={
                "securitySchemes": [{
                    "type": "oauth2",
                    "scopes": [required_scope(name)],
                }],
                "cw/capability": contract.capability,
                "cw/protocolVersion": PROTOCOL_VERSION,
            },
            structured_output=True,
        )(function)

    async def project_status(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_project_status", {"project_id": project_id, "operation_id": operation_id})

    async def project_inspect(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_project_inspect", {"project_id": project_id, "operation_id": operation_id})

    async def history(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_history", {"project_id": project_id, "operation_id": operation_id})

    async def explain(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_explain", {"project_id": project_id, "operation_id": operation_id})

    async def completion(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_completion_status", {"project_id": project_id, "operation_id": operation_id})

    async def gates(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_gate_status", {"project_id": project_id, "operation_id": operation_id})

    async def phase_start(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_phase_start", {"project_id": project_id, "operation_id": operation_id})

    async def validate(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_validate", {"project_id": project_id, "operation_id": operation_id})

    async def review(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_request_review", {"project_id": project_id, "operation_id": operation_id})

    async def retry(project_id: str, operation_id: str = "") -> dict[str, Any]:
        return await invoke("cw_retry", {"project_id": project_id, "operation_id": operation_id})

    async def operation_status(
        target_operation_id: str, project_id: str, operation_id: str = "",
    ) -> dict[str, Any]:
        return await invoke("cw_operation_status", {
            "project_id": project_id,
            "operation_id": operation_id,
            "target_operation_id": target_operation_id,
        })

    async def operation_cancel(
        target_operation_id: str, project_id: str, operation_id: str = "",
    ) -> dict[str, Any]:
        return await invoke("cw_operation_cancel", {
            "project_id": project_id,
            "operation_id": operation_id,
            "target_operation_id": target_operation_id,
        })

    functions = {
        "cw_project_status": project_status,
        "cw_project_inspect": project_inspect,
        "cw_history": history,
        "cw_explain": explain,
        "cw_completion_status": completion,
        "cw_gate_status": gates,
        "cw_phase_start": phase_start,
        "cw_validate": validate,
        "cw_request_review": review,
        "cw_retry": retry,
        "cw_operation_status": operation_status,
        "cw_operation_cancel": operation_cancel,
    }
    for tool_name in sorted(REMOTE_TOOLS):
        register(tool_name, functions[tool_name])

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(_: Any) -> Any:
        return JSONResponse({
            "status": "ok",
            "service": "cw-remote-gateway",
            "build": identity.to_dict(),
        })

    @server.custom_route("/readyz", methods=["GET"], include_in_schema=False)
    async def readiness(_: Any) -> Any:
        ready = service.store.schema_version() == 1
        return JSONResponse({
            "status": "ready" if ready else "not_ready",
            "service": "cw-remote-gateway",
            "schema_version": service.store.schema_version(),
            "build": identity.to_dict(),
        }, status_code=200 if ready else 503)

    @server.custom_route("/.well-known/oauth-protected-resource", methods=["GET"], include_in_schema=False)
    async def resource_metadata(_: Any) -> Any:
        return JSONResponse(protected_resource_metadata(oauth), headers={"cache-control": "public, max-age=300"})

    @server.custom_route("/remote/v1/pairing/request", methods=["POST"], include_in_schema=False)
    async def pairing_request(request: Any) -> Any:
        try:
            payload = await _bounded_json(request, service.router.limits.maximum_request_bytes)
            device_id = str(payload.get("device_id", ""))
            network = getattr(getattr(request, "client", None), "host", "unknown")
            await service.router.check_pairing_rate(
                f"pairing-network:{network}", f"pairing-device:{device_id}",
            )
            challenge = service.pairing.request_public(
                device_id=device_id,
                public_key=str(payload.get("public_key", "")),
                display_name=str(payload.get("display_name", "")),
            )
            return JSONResponse({
                "challenge_id": challenge.challenge_id,
                "user_code": challenge.user_code,
                "device_id": challenge.device_id,
                "display_name": challenge.display_name,
                "expires_at": challenge.expires_at,
            }, status_code=201, headers={"cache-control": "no-store"})
        except RemoteError as exc:
            return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    @server.custom_route("/remote/v1/pairing/confirm", methods=["POST"], include_in_schema=False)
    async def pairing_confirm(request: Any) -> Any:
        try:
            payload = await _bounded_json(request, service.router.limits.maximum_request_bytes)
            identity = current_identity()
            device = service.pairing.confirm(
                challenge_id=str(payload.get("challenge_id", "")),
                user_code=str(payload.get("user_code", "")),
                principal_id=identity.principal_id,
                workspace_id=identity.workspace_id,
            )
            return JSONResponse({
                "device_id": device.device_id,
                "workspace_id": device.workspace_id,
                "status": "PAIRED",
            }, headers={"cache-control": "no-store"})
        except RemoteError as exc:
            return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    @server.custom_route("/remote/v1/agent/poll", methods=["POST"], include_in_schema=False)
    async def agent_poll(request: Any) -> Any:
        try:
            body, payload, device_id = await _verified_agent_request(request, service)
            timeout = payload.get("timeout_seconds", 20)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Agent poll timeout is invalid")
            routed = await service.router.poll(device_id, timeout_seconds=float(timeout))
            return JSONResponse({"request": routed.to_dict() if routed else None})
        except RemoteError as exc:
            return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    @server.custom_route("/remote/v1/agent/respond", methods=["POST"], include_in_schema=False)
    async def agent_respond(request: Any) -> Any:
        try:
            _, payload, device_id = await _verified_agent_request(request, service)
            response = RemoteResponse.from_dict(payload)
            await service.router.accept_response(device_id, response)
            return JSONResponse({"accepted": True, "request_id": response.request_id})
        except RemoteError as exc:
            return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    @server.custom_route("/remote/v1/agent/grants", methods=["POST"], include_in_schema=False)
    async def agent_grant(request: Any) -> Any:
        try:
            _, payload, device_id = await _verified_agent_request(request, service)
            display_name = payload.get("display_name")
            if not isinstance(display_name, str) or not display_name or len(display_name) > 120:
                raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Project display name is invalid")
            grant = service.create_project_grant(device_id=device_id, display_name=display_name)
            return JSONResponse({
                "project_handle": grant.project_handle,
                "display_name": grant.display_name,
                "principal_id": grant.principal_id,
                "workspace_id": grant.workspace_id,
                "device_id": grant.device_id,
                "status": "GRANTED",
            }, status_code=201)
        except RemoteError as exc:
            return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    app = server.streamable_http_app()
    return OAuthResourceMiddleware(
        app,
        service.verifier,
        public_paths={
            "/healthz", "/readyz", "/.well-known/oauth-protected-resource",
            "/remote/v1/pairing/request",
        },
    )


async def _bounded_json(request: Any, maximum: int) -> dict[str, Any]:
    body = await request.body()
    if len(body) > maximum:
        raise RemoteError(RemoteErrorCode.REQUEST_TOO_LARGE, "Remote request is too large", http_status=413)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote request must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote request must be an object")
    return payload


async def _verified_agent_request(request: Any, service: GatewayService) -> tuple[bytes, dict[str, Any], str]:
    body = await request.body()
    if len(body) > service.router.limits.maximum_agent_message_bytes:
        raise RemoteError(RemoteErrorCode.REQUEST_TOO_LARGE, "Agent message is too large", http_status=413)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Agent message must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Agent message must be an object")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise RemoteError(
            RemoteErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
            "The gateway and agent protocol versions are incompatible",
            http_status=409,
        )
    device_id = request.headers.get("x-cw-device-id", "")
    verify_device_signature(
        service.store,
        device_id=device_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=request.headers.get("x-cw-timestamp", ""),
        nonce=request.headers.get("x-cw-nonce", ""),
        signature=request.headers.get("x-cw-signature", ""),
    )
    return body, payload, device_id


def serve_gateway(app: Any, *, host: str, port: int) -> int:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        print("The remote gateway requires codex-workflow[remote]", file=sys.stderr)
        return 2
    # TLS is intentionally supplied by a production reverse proxy or by
    # uvicorn's deployment wrapper.  CW does not invent certificates.
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0
