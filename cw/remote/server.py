from __future__ import annotations

import os
import base64
import hashlib
import hmac
import html
import asyncio
import json
import re
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

from cw import __version__
from cw.adapters.mcp.runtime import TOOLS
from cw.core.utils import utc_now

from .auth import (
    discover_authorization_server,
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
USER_CODE_PATTERN = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")


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


@dataclass(frozen=True, slots=True)
class PairingWebConfig:
    """Browser OAuth configuration for human device-pairing confirmation."""

    client_id: str
    redirect_uri: str
    session_secret: str
    route: str = "/remote/pair"
    scopes: tuple[str, ...] = ("project.read",)
    session_lifetime_seconds: int = 900
    cookie_name: str = "cw_pairing_session"
    oauth_cookie_name: str = "cw_pairing_oauth"

    def __post_init__(self) -> None:
        if not self.client_id or len(self.client_id) > 256:
            raise ValueError("Pairing OAuth client identifier is invalid")
        parsed = urlparse(self.redirect_uri)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Pairing OAuth redirect URI must be an absolute HTTPS URL without query or fragment")
        if self.route != "/" + self.route.strip("/"):
            raise ValueError("Pairing route must be an absolute normalized path")
        if len(self.session_secret) < 32:
            raise ValueError("Pairing session secret must be at least 32 characters")
        if not self.scopes or any(not item or " " in item for item in self.scopes):
            raise ValueError("Pairing OAuth scopes are invalid")


def create_gateway_app(
    service: GatewayService,
    oauth: OAuthResourceConfig,
    *,
    runtime_identity: GatewayRuntimeIdentity | None = None,
    allowed_hosts: tuple[str, ...] = (),
    pairing_web: PairingWebConfig | None = None,
) -> Any:
    """Create a hosting-neutral ASGI application with Streamable HTTP at /mcp."""

    FastMCP, ToolAnnotations, _, JSONResponse = _dependencies()
    from starlette.responses import HTMLResponse, RedirectResponse
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

    if pairing_web is not None:
        login_path = pairing_web.route.rstrip("/") + "/login"
        callback_path = pairing_web.route.rstrip("/") + "/callback"

        @server.custom_route(pairing_web.route, methods=["GET"], include_in_schema=False)
        async def pairing_page(request: Any) -> Any:
            session = _pairing_session_from_request(request, pairing_web)
            if session is None:
                return _redirect_to_login(pairing_web, request)
            code = _normalized_user_code(str(request.query_params.get("code", "")))
            csrf = str(session["csrf"])
            if not code:
                return HTMLResponse(_pairing_html(
                    title="CW - Device Pairing",
                    body=(
                        "<form method='get'>"
                        "<label>User code</label>"
                        "<input name='code' autocomplete='one-time-code' pattern='[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}' required>"
                        "<button type='submit'>Continue</button>"
                        "</form>"
                    ),
                ), headers={"cache-control": "no-store"})
            try:
                record = service.pairing.pending_by_user_code(code)
            except RemoteError as exc:
                return HTMLResponse(_pairing_error_html(exc.message), status_code=exc.http_status, headers={"cache-control": "no-store"})
            return HTMLResponse(_pairing_html(
                title="CW - Device Pairing",
                body=(
                    "<dl>"
                    f"<dt>Device</dt><dd>{html.escape(str(record['display_name']))}</dd>"
                    f"<dt>Device ID</dt><dd>{html.escape(_short_identifier(str(record['device_id'])))}</dd>"
                    f"<dt>Workspace</dt><dd>{html.escape(str(session['workspace_id']))}</dd>"
                    "<dt>Requested action</dt><dd>Pair this CW device</dd>"
                    f"<dt>Expires</dt><dd>{html.escape(str(record['expires_at']))}</dd>"
                    f"<dt>User code</dt><dd>{html.escape(code)}</dd>"
                    "</dl>"
                    "<form method='post'>"
                    f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
                    f"<input type='hidden' name='code' value='{html.escape(code)}'>"
                    "<button type='submit' name='decision' value='approve'>Approve</button>"
                    "<button type='submit' name='decision' value='reject'>Reject</button>"
                    "</form>"
                ),
            ), headers={"cache-control": "no-store"})

        @server.custom_route(pairing_web.route, methods=["POST"], include_in_schema=False)
        async def pairing_decision(request: Any) -> Any:
            session = _pairing_session_from_request(request, pairing_web)
            if session is None:
                return _redirect_to_login(pairing_web, request)
            body = await request.body()
            if len(body) > 8192:
                return HTMLResponse(_pairing_error_html("Pairing confirmation request is too large"), status_code=413, headers={"cache-control": "no-store"})
            form_values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            form = {key: values[-1] if values else "" for key, values in form_values.items()}
            if not hmac.compare_digest(str(form.get("csrf", "")), str(session["csrf"])):
                return HTMLResponse(_pairing_error_html("Pairing confirmation state is invalid"), status_code=403, headers={"cache-control": "no-store"})
            code = _normalized_user_code(str(form.get("code", "")))
            decision = str(form.get("decision", ""))
            try:
                record = service.pairing.pending_by_user_code(code)
                if decision == "approve":
                    device = service.pairing.confirm(
                        challenge_id=str(record["challenge_id"]),
                        user_code=code,
                        principal_id=str(session["principal_id"]),
                        workspace_id=str(session["workspace_id"]),
                    )
                    return HTMLResponse(_pairing_html(
                        title="CW - Device Paired",
                        body=(
                            "<p>Device pairing approved.</p>"
                            f"<p>Device: {html.escape(_short_identifier(device.device_id))}</p>"
                        ),
                    ), headers={"cache-control": "no-store"})
                if decision == "reject":
                    service.pairing.reject(
                        challenge_id=str(record["challenge_id"]),
                        user_code=code,
                        principal_id=str(session["principal_id"]),
                        workspace_id=str(session["workspace_id"]),
                    )
                    return HTMLResponse(_pairing_html(
                        title="CW - Device Pairing Rejected",
                        body="<p>Device pairing rejected. This code cannot be reused.</p>",
                    ), headers={"cache-control": "no-store"})
                raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Pairing decision is invalid", http_status=400)
            except RemoteError as exc:
                return HTMLResponse(_pairing_error_html(exc.message), status_code=exc.http_status, headers={"cache-control": "no-store"})

        @server.custom_route(login_path, methods=["GET"], include_in_schema=False)
        async def pairing_login(request: Any) -> Any:
            code = _normalized_user_code(str(request.query_params.get("code", "")))
            state = secrets.token_urlsafe(24)
            verifier = secrets.token_urlsafe(48)
            challenge = _pkce_challenge(verifier)
            payload = {
                "state": state,
                "verifier": verifier,
                "code": code,
                "exp": _epoch_seconds(pairing_web.session_lifetime_seconds),
            }
            try:
                metadata = await discover_authorization_server(oauth.issuer)
            except RemoteError as exc:
                return HTMLResponse(_pairing_error_html(exc.message), status_code=exc.http_status, headers={"cache-control": "no-store"})
            query = urlencode({
                "response_type": "code",
                "client_id": pairing_web.client_id,
                "redirect_uri": pairing_web.redirect_uri,
                "scope": " ".join(pairing_web.scopes),
                "audience": oauth.resource,
                "resource": oauth.resource,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            })
            response = RedirectResponse(metadata.authorization_endpoint + "?" + query, status_code=303)
            _set_signed_cookie(response, pairing_web.oauth_cookie_name, payload, pairing_web, request)
            return response

        @server.custom_route(callback_path, methods=["GET"], include_in_schema=False)
        async def pairing_callback(request: Any) -> Any:
            oauth_session = _signed_cookie(request, pairing_web.oauth_cookie_name, pairing_web)
            if oauth_session is None or not hmac.compare_digest(
                str(oauth_session.get("state", "")), str(request.query_params.get("state", "")),
            ):
                return HTMLResponse(_pairing_error_html("OAuth callback state is invalid"), status_code=401, headers={"cache-control": "no-store"})
            code_value = str(request.query_params.get("code", ""))
            if not code_value:
                return HTMLResponse(_pairing_error_html("OAuth authorization code is missing"), status_code=401, headers={"cache-control": "no-store"})
            try:
                metadata = await discover_authorization_server(oauth.issuer)
                token_payload = await _exchange_oauth_code(
                    token_endpoint=metadata.token_endpoint,
                    code=code_value,
                    redirect_uri=pairing_web.redirect_uri,
                    client_id=pairing_web.client_id,
                    code_verifier=str(oauth_session["verifier"]),
                    resource=oauth.resource,
                )
                identity = await _verify_oauth_access_token(service, token_payload)
            except (RemoteError, KeyError) as exc:
                message = exc.message if isinstance(exc, RemoteError) else "OAuth token response is invalid"
                return HTMLResponse(_pairing_error_html(message), status_code=401, headers={"cache-control": "no-store"})
            session_payload = {
                "principal_id": identity.principal_id,
                "workspace_id": identity.workspace_id,
                "client_id": identity.client_id,
                "scopes": sorted(identity.scopes),
                "token_id": identity.token_id,
                "csrf": secrets.token_urlsafe(24),
                "exp": _epoch_seconds(pairing_web.session_lifetime_seconds),
            }
            destination = pairing_web.route
            code = str(oauth_session.get("code", ""))
            if code:
                destination += "?" + urlencode({"code": code})
            response = RedirectResponse(destination, status_code=303)
            _set_signed_cookie(response, pairing_web.cookie_name, session_payload, pairing_web, request)
            response.delete_cookie(pairing_web.oauth_cookie_name, path="/", samesite="lax")
            return response

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
    public_paths = {
        "/healthz", "/readyz", "/.well-known/oauth-protected-resource",
        "/remote/v1/pairing/request",
    }
    if pairing_web is not None:
        public_paths.update({
            pairing_web.route,
            pairing_web.route.rstrip("/") + "/login",
            pairing_web.route.rstrip("/") + "/callback",
        })
    return OAuthResourceMiddleware(
        app,
        service.verifier,
        public_paths=public_paths,
    )


def _normalized_user_code(value: str) -> str:
    normalized = value.strip().upper()
    return normalized if USER_CODE_PATTERN.fullmatch(normalized) else ""


def _short_identifier(value: str) -> str:
    if len(value) <= 16:
        return value
    return value[:8] + "..." + value[-6:]


def _pairing_html(*, title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escaped_title}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#111827}"
        "main{max-width:42rem;margin:10vh auto;padding:2rem;background:white;border:1px solid #d1d5db;border-radius:8px}"
        "h1{font-size:1.35rem;margin:0 0 1.25rem}label,dt{font-weight:650}dd{margin:0 0 .75rem}"
        "input{display:block;width:100%;box-sizing:border-box;margin:.5rem 0 1rem;padding:.7rem;border:1px solid #9ca3af;border-radius:6px}"
        "button{margin:.25rem .5rem .25rem 0;padding:.65rem 1rem;border:1px solid #374151;border-radius:6px;background:#111827;color:white}"
        "button[value=reject]{background:white;color:#111827}"
        "p{line-height:1.5}"
        "</style></head><body><main>"
        f"<h1>{escaped_title}</h1>{body}</main></body></html>"
    )


def _pairing_error_html(message: str) -> str:
    return _pairing_html(
        title="CW - Device Pairing",
        body=f"<p>{html.escape(message)}</p>",
    )


def _epoch_seconds(lifetime_seconds: int) -> int:
    return int((datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)).timestamp())


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _sign_cookie(payload: Mapping[str, Any], secret: str) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def _unsign_cookie(value: str, secret: str) -> dict[str, Any] | None:
    try:
        encoded, supplied = value.split(".", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest(),
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(datetime.now(timezone.utc).timestamp()):
        return None
    return payload


def _signed_cookie(request: Any, name: str, config: PairingWebConfig) -> dict[str, Any] | None:
    raw = request.cookies.get(name)
    if not isinstance(raw, str):
        return None
    return _unsign_cookie(raw, config.session_secret)


def _secure_cookie(request: Any) -> bool:
    host = getattr(request.url, "hostname", "") or ""
    return host not in {"127.0.0.1", "localhost"}


def _set_signed_cookie(response: Any, name: str, payload: Mapping[str, Any], config: PairingWebConfig, request: Any) -> None:
    response.set_cookie(
        name,
        _sign_cookie(payload, config.session_secret),
        max_age=config.session_lifetime_seconds,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path="/",
    )


def _pairing_session_from_request(request: Any, config: PairingWebConfig) -> dict[str, Any] | None:
    session = _signed_cookie(request, config.cookie_name, config)
    if session is None:
        return None
    required = ("principal_id", "workspace_id", "client_id", "csrf")
    if not all(isinstance(session.get(item), str) and session[item] for item in required):
        return None
    scopes = session.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        return None
    return session


def _redirect_to_login(config: PairingWebConfig, request: Any) -> Any:
    from starlette.responses import RedirectResponse

    code = _normalized_user_code(str(request.query_params.get("code", "")))
    destination = config.route.rstrip("/") + "/login"
    if code:
        destination += "?" + urlencode({"code": code})
    return RedirectResponse(destination, status_code=303)


async def _exchange_oauth_code(
    *, token_endpoint: str, code: str, redirect_uri: str, client_id: str,
    code_verifier: str, resource: str,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.post(token_endpoint, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "resource": resource,
        })
    if response.status_code >= 400:
        raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "OAuth authorization code exchange failed", http_status=401)
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "OAuth token response is invalid", http_status=401) from exc
    if not isinstance(payload, dict):
        raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "OAuth token response is invalid", http_status=401)
    return payload


async def _verify_oauth_access_token(service: GatewayService, payload: Mapping[str, Any]) -> Any:
    token = payload.get("access_token")
    token_type = str(payload.get("token_type", "Bearer"))
    if not isinstance(token, str) or token_type.lower() != "bearer":
        raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "OAuth access token response is invalid", http_status=401)
    return await asyncio.to_thread(service.verifier.verify, token)


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
