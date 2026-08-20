from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cw.adapters.mcp.runtime import MCPRuntime, RuntimeConfig
from cw.application import Actor, ActorOrigin

from .device import DeviceCredential, signed_headers
from .errors import RemoteError, RemoteErrorCode
from .gateway import GatewayService
from .protocol import RemoteRequest, RemoteResponse


def _replace_identity(value: Any, local_id: str, remote_id: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace_identity(item, local_id, remote_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_identity(item, local_id, remote_id) for item in value]
    if isinstance(value, str) and value == local_id:
        return remote_id
    return value


@dataclass(frozen=True, slots=True)
class LocalProjectGrant:
    project_handle: str
    project_path: Path
    local_project_id: str
    display_name: str
    principal_id: str | None = None
    workspace_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class LocalAgentState:
    """Local-only mapping from opaque remote handles to canonical repositories."""

    grants: dict[str, dict[str, str]]
    schema_version: int = 1

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "schema_version": self.schema_version,
            "grants": self.grants,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    @classmethod
    def load(cls, path: Path) -> "LocalAgentState":
        if not path.is_file():
            return cls({})
        if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Remote agent state permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "grants"}
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("grants"), dict)
            or not all(
                isinstance(key, str)
                and isinstance(value, dict)
                and set(value) == {"project_path", "principal_id", "workspace_id", "device_id", "display_name"}
                and all(isinstance(item, str) and item for item in value.values())
                for key, value in payload["grants"].items()
            )
        ):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Remote agent state is invalid")
        return cls(dict(payload["grants"]))


class LocalAgentRuntime:
    """Closed local dispatcher from remote protocol into the shared MCP runtime."""

    def __init__(
        self, *, project_paths: list[Path] | tuple[Path, ...],
        allowed_roots: list[Path] | tuple[Path, ...],
        grant_handles: Mapping[Path | str, str], review_backend_factory: object | None = None,
        operation_workers: int = 2,
        grant_identities: Mapping[str, tuple[str, str, str]] | None = None,
    ) -> None:
        config = RuntimeConfig.create(
            project_paths,
            allowed_roots,
            actor=Actor("remote-plugin", ActorOrigin.CHATGPT_APP),
            surface="remote-gateway",
        )
        self.runtime = MCPRuntime(
            config,
            diagnostic_sink=lambda _: None,
            review_backend_factory=review_backend_factory,
            operation_workers=operation_workers,
        )
        grants: dict[str, LocalProjectGrant] = {}
        for path_value, remote_handle in grant_handles.items():
            path = Path(path_value).resolve(strict=True)
            display_name = path.name
            try:
                local_id = self.runtime.application.open_project(path).handle.repository_id
            except Exception as exc:
                raise RemoteError(
                    RemoteErrorCode.PROJECT_SCOPE_VIOLATION,
                    "Local project grant is not in the agent startup scope",
                ) from exc
            identity = (grant_identities or {}).get(remote_handle)
            grants[remote_handle] = LocalProjectGrant(
                remote_handle, path, local_id, display_name,
                *(identity or (None, None, None)),
            )
        if set(grants) != set(grant_handles.values()):
            raise RemoteError(RemoteErrorCode.PROJECT_SCOPE_VIOLATION, "Local project grant handles are not unique")
        self.grants = grants

    def execute(self, request: RemoteRequest) -> RemoteResponse:
        request.validate()
        grant = self.grants.get(request.project_handle)
        if grant is None:
            return RemoteResponse(
                request.request_id, request.operation_id, request.project_handle, "FAILED",
                error={
                    "code": RemoteErrorCode.PROJECT_NOT_GRANTED.value,
                    "message": "Project is not granted by this local agent",
                    "retryable": False,
                    "details": {},
                },
            )
        if grant.principal_id is not None and (
            request.principal_id != grant.principal_id
            or request.workspace_id != grant.workspace_id
            or request.device_id != grant.device_id
        ):
            return RemoteResponse(
                request.request_id, request.operation_id, request.project_handle, "FAILED",
                error={
                    "code": RemoteErrorCode.PROJECT_SCOPE_VIOLATION.value,
                    "message": "Remote request identity does not match the local project grant",
                    "retryable": False,
                    "details": {},
                },
            )
        deadline = datetime.fromisoformat(request.deadline_at.replace("Z", "+00:00"))
        if deadline <= datetime.now(timezone.utc):
            return RemoteResponse(
                request.request_id, request.operation_id, request.project_handle, "FAILED",
                error={
                    "code": RemoteErrorCode.OPERATION_TIMEOUT.value,
                    "message": "Remote request expired before local execution",
                    "retryable": True,
                    "details": {},
                },
            )
        arguments = dict(request.arguments)
        arguments["project_id"] = grant.local_project_id
        result = self.runtime.call_tool(request.tool, arguments)
        result = _replace_identity(result, grant.local_project_id, request.project_handle)
        if result.get("status") in {"FAILED", "BLOCKED"} and isinstance(result.get("error"), dict):
            return RemoteResponse(
                request.request_id, request.operation_id, request.project_handle,
                str(result["status"]), error=dict(result["error"]),
            )
        return RemoteResponse(
            request.request_id, request.operation_id, request.project_handle,
            str(result.get("status", "SUCCEEDED")), result=result,
        )

    def shutdown(self, *, wait: bool = True) -> None:
        self.runtime.shutdown(wait=wait)


class InProcessAgent:
    """Deterministic agent transport used by the multi-process contract harness."""

    def __init__(self, service: GatewayService, device_id: str, runtime: LocalAgentRuntime) -> None:
        self.service = service
        self.device_id = device_id
        self.runtime = runtime
        self._running = False

    async def connect(self) -> None:
        await self.service.router.connect_agent(self.device_id)
        self._running = True

    async def run_once(self, *, timeout_seconds: float = 0.1) -> bool:
        request = await self.service.router.poll(self.device_id, timeout_seconds=timeout_seconds)
        if request is None:
            return False
        response = await asyncio.to_thread(self.runtime.execute, request)
        await self.service.router.accept_response(self.device_id, response)
        return True

    async def disconnect(self) -> None:
        self._running = False
        await self.service.router.disconnect_agent(self.device_id)


class HTTPAgentClient:
    """Outbound-only long-poll transport for a paired local CW agent."""

    def __init__(
        self, *, gateway_url: str, credential: DeviceCredential,
        runtime: LocalAgentRuntime, poll_seconds: float = 20.0,
    ) -> None:
        if not gateway_url.startswith("https://") and not gateway_url.startswith("http://127.0.0.1"):
            raise ValueError("Remote gateway must use HTTPS outside loopback development")
        self.gateway_url = gateway_url.rstrip("/")
        self.credential = credential
        self.runtime = runtime
        self.poll_seconds = poll_seconds

    async def run(self, stop: asyncio.Event) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
        delay = 0.25
        async with httpx.AsyncClient(timeout=self.poll_seconds + 5) as client:
            while not stop.is_set():
                try:
                    request = await self._poll(client)
                    if request is None:
                        delay = 0.25
                        continue
                    response = await asyncio.to_thread(self.runtime.execute, request)
                    await self._respond(client, response)
                    delay = 0.25
                except httpx.HTTPStatusError as exc:
                    # Authentication, device revocation, and project revocation
                    # fail closed.  Retrying them forever would hide an explicit
                    # operator action behind an apparently healthy reconnect loop.
                    if exc.response.status_code in {401, 403}:
                        raise RemoteError(
                            RemoteErrorCode.DEVICE_REVOKED,
                            "The remote agent credential or grant is no longer authorized",
                            http_status=exc.response.status_code,
                        ) from exc
                    await asyncio.sleep(delay)
                    delay = min(5.0, delay * 2)
                except (httpx.HTTPError, RemoteError):
                    await asyncio.sleep(delay)
                    delay = min(5.0, delay * 2)

    async def _poll(self, client: Any) -> RemoteRequest | None:
        path = "/remote/v1/agent/poll"
        body = json.dumps({
            "protocol_version": "cw.remote.v1",
            "timeout_seconds": self.poll_seconds,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        response = await client.post(
            self.gateway_url + path,
            content=body,
            headers={"content-type": "application/json", **signed_headers(
                self.credential, method="POST", path=path, body=body,
            )},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("request") is None:
            return None
        return RemoteRequest.from_dict(payload["request"])

    async def _respond(self, client: Any, result: RemoteResponse) -> None:
        path = "/remote/v1/agent/respond"
        body = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        response = await client.post(
            self.gateway_url + path,
            content=body,
            headers={"content-type": "application/json", **signed_headers(
                self.credential, method="POST", path=path, body=body,
            )},
        )
        response.raise_for_status()


async def request_pairing(
    *, gateway_url: str, credential: DeviceCredential, display_name: str,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
    if not gateway_url.startswith("https://") and not gateway_url.startswith("http://127.0.0.1"):
        raise ValueError("Remote gateway must use HTTPS outside loopback development")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(gateway_url.rstrip("/") + "/remote/v1/pairing/request", json={
            "device_id": credential.device_id,
            "public_key": credential.public_key,
            "display_name": display_name,
        })
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RemoteError(RemoteErrorCode.REMOTE_TRANSPORT_UNAVAILABLE, "Pairing response is invalid")
        return payload


async def register_project_grant(
    *, gateway_url: str, credential: DeviceCredential, project: Path,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
    canonical = project.resolve(strict=True)
    path = "/remote/v1/agent/grants"
    body = json.dumps({
        "display_name": canonical.name,
        "protocol_version": "cw.remote.v1",
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            gateway_url.rstrip("/") + path,
            content=body,
            headers={"content-type": "application/json", **signed_headers(
                credential, method="POST", path=path, body=body,
            )},
        )
        response.raise_for_status()
        payload = response.json()
        required = {"project_handle", "display_name", "principal_id", "workspace_id", "device_id", "status"}
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or not all(isinstance(payload.get(item), str) for item in required)
        ):
            raise RemoteError(RemoteErrorCode.REMOTE_TRANSPORT_UNAVAILABLE, "Project grant response is invalid")
        return payload
