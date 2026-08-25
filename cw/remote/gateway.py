from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Mapping

from cw.core.utils import utc_now

from .auth import OAuthTokenVerifier
from .device import PairingService
from .errors import RemoteError, RemoteErrorCode
from .persistence import ProjectGrantRecord, RemoteStore
from .protocol import (
    RemoteIdentity,
    RemoteRequest,
    RemoteResponse,
    https_read_only_tool_contract,
    required_scope,
)


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    requests_per_minute: int = 120
    requests_per_device_per_minute: int = 240
    pairing_requests_per_minute: int = 20
    concurrent_requests_per_device: int = 4
    maximum_request_bytes: int = 64 * 1024
    maximum_agent_message_bytes: int = 512 * 1024
    operation_timeout_seconds: float = 30.0
    agent_idle_seconds: float = 45.0
    completed_response_cache_size: int = 1024
    concurrent_http_requests: int = 32
    http_queue_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if min(
            self.requests_per_minute,
            self.requests_per_device_per_minute,
            self.pairing_requests_per_minute,
            self.concurrent_requests_per_device,
            self.maximum_request_bytes,
            self.maximum_agent_message_bytes,
            self.completed_response_cache_size,
            self.concurrent_http_requests,
        ) <= 0 or min(
            self.operation_timeout_seconds,
            self.agent_idle_seconds,
            self.http_queue_timeout_seconds,
        ) <= 0:
            raise ValueError("Gateway limits must be positive")


class _RateLimiter:
    def __init__(self, limit: int, *, maximum_keys: int = 10_000) -> None:
        self.limit = limit
        self.maximum_keys = maximum_keys
        self._windows: OrderedDict[str, Deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        async with self._lock:
            window = self._windows.get(key)
            if window is None:
                while len(self._windows) >= self.maximum_keys:
                    self._windows.popitem(last=False)
                window = deque()
                self._windows[key] = window
            else:
                self._windows.move_to_end(key)
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.limit:
                raise RemoteError(
                    RemoteErrorCode.RATE_LIMITED,
                    "Remote CW request rate limit exceeded",
                    retryable=True,
                    http_status=429,
                )
            window.append(now)


@dataclass(slots=True)
class _Pending:
    request: RemoteRequest
    future: asyncio.Future[RemoteResponse]


class RemoteRouter:
    """Tenant-scoped request router for outbound-only local agents."""

    def __init__(
        self, store: RemoteStore, verifier: OAuthTokenVerifier, *,
        limits: GatewayLimits | None = None,
    ) -> None:
        self.store = store
        self.verifier = verifier
        self.limits = limits or GatewayLimits()
        self._queues: dict[str, asyncio.Queue[RemoteRequest]] = {}
        self._last_seen: dict[str, float] = {}
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._completed: OrderedDict[tuple[str, str], RemoteResponse] = OrderedDict()
        self._inflight_by_device: dict[str, int] = defaultdict(int)
        self._guard = asyncio.Lock()
        self._principal_rate = _RateLimiter(self.limits.requests_per_minute)
        self._device_rate = _RateLimiter(self.limits.requests_per_device_per_minute)
        self._pairing_rate = _RateLimiter(self.limits.pairing_requests_per_minute)

    async def check_pairing_rate(self, *keys: str) -> None:
        for key in keys:
            await self._pairing_rate.check(key)

    async def connect_agent(self, device_id: str) -> None:
        device = self.store.device(device_id)
        if device is None:
            raise RemoteError(RemoteErrorCode.DEVICE_NOT_PAIRED, "Device is not paired", http_status=401)
        if device.revoked_at is not None:
            raise RemoteError(RemoteErrorCode.DEVICE_REVOKED, "Device is revoked", http_status=403)
        async with self._guard:
            self._queues.setdefault(device_id, asyncio.Queue())
            self._last_seen[device_id] = time.monotonic()
        self.store.audit(
            "agent_connected", outcome="AVAILABLE", device_id=device_id,
            principal_id=device.principal_id, workspace_id=device.workspace_id,
        )

    async def disconnect_agent(self, device_id: str) -> None:
        async with self._guard:
            self._last_seen.pop(device_id, None)
        self.store.audit("agent_disconnected", outcome="OFFLINE", device_id=device_id)

    async def agent_available(self, device_id: str) -> bool:
        async with self._guard:
            seen = self._last_seen.get(device_id)
            return seen is not None and time.monotonic() - seen <= self.limits.agent_idle_seconds

    async def poll(self, device_id: str, *, timeout_seconds: float = 20.0) -> RemoteRequest | None:
        if not await self.agent_available(device_id):
            await self.connect_agent(device_id)
        async with self._guard:
            queue = self._queues.setdefault(device_id, asyncio.Queue())
            self._last_seen[device_id] = time.monotonic()
        poll_deadline = time.monotonic() + min(timeout_seconds, self.limits.agent_idle_seconds)
        while True:
            remaining = poll_deadline - time.monotonic()
            if remaining <= 0:
                async with self._guard:
                    self._last_seen[device_id] = time.monotonic()
                return None
            try:
                request = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                async with self._guard:
                    self._last_seen[device_id] = time.monotonic()
                return None
            async with self._guard:
                pending = self._pending.get((request.workspace_id, request.request_id))
                if pending is not None and pending.request == request:
                    break
            # A timed-out request can remain queued until the agent's next poll.
            # Drop that stale delivery; a client replay will enqueue the same
            # operation with the original digest and local idempotency key.
        self.store.update_routed_state(request.workspace_id, request.request_id, "DELIVERED", utc_now())
        self.store.audit(
            "operation_started", outcome="DELIVERED", request_id=request.request_id,
            operation_id=request.operation_id, principal_id=request.principal_id,
            workspace_id=request.workspace_id, device_id=device_id,
            project_handle=request.project_handle, capability=request.capability,
            actor=request.actor, origin=request.origin,
        )
        return request

    async def dispatch(
        self, identity: RemoteIdentity, *, project_handle: str, tool: str,
        arguments: Mapping[str, Any], request_id: str | None = None,
        operation_id: str | None = None, timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        scope = required_scope(tool)
        self.verifier.require_scope(identity, scope)
        await self._principal_rate.check(f"{identity.workspace_id}:{identity.principal_id}")
        try:
            grant = self.store.resolve_project_grant(
                project_handle=project_handle,
                principal_id=identity.principal_id,
                workspace_id=identity.workspace_id,
            )
        except RemoteError:
            self.store.audit(
                "project_scope_violation", outcome="DENIED",
                principal_id=identity.principal_id, workspace_id=identity.workspace_id,
                project_handle=project_handle, capability=scope,
            )
            raise
        self.store.audit(
            "capability_allowed", outcome="ALLOWED",
            principal_id=identity.principal_id, workspace_id=identity.workspace_id,
            device_id=grant.device_id, project_handle=project_handle,
            capability=scope, actor=identity.principal_id, origin="remote_plugin",
        )
        await self._device_rate.check(f"{identity.workspace_id}:{grant.device_id}")
        if not await self.agent_available(grant.device_id):
            self.store.audit(
                "tool_requested", outcome=RemoteErrorCode.AGENT_OFFLINE.value,
                principal_id=identity.principal_id, workspace_id=identity.workspace_id,
                device_id=grant.device_id, project_handle=project_handle,
                capability=scope,
            )
            raise RemoteError(
                RemoteErrorCode.AGENT_OFFLINE,
                "The authorized CW agent is offline",
                retryable=True,
                http_status=503,
            )
        request_id = request_id or uuid.uuid4().hex
        operation_id = operation_id or (
            arguments.get("operation_id") if isinstance(arguments.get("operation_id"), str) else uuid.uuid4().hex
        )
        created = datetime.now(timezone.utc).replace(microsecond=0)
        deadline = created + timedelta(seconds=timeout_seconds or self.limits.operation_timeout_seconds)
        request = RemoteRequest.create(
            request_id=request_id,
            operation_id=operation_id,
            identity=identity,
            device_id=grant.device_id,
            project_handle=project_handle,
            tool=tool,
            arguments=arguments,
            created_at=created.isoformat().replace("+00:00", "Z"),
            deadline_at=deadline.isoformat().replace("+00:00", "Z"),
        )
        encoded_size = len(json.dumps(request.to_dict(), separators=(",", ":")).encode("utf-8"))
        if encoded_size > self.limits.maximum_request_bytes:
            raise RemoteError(RemoteErrorCode.REQUEST_TOO_LARGE, "Remote CW request is too large", http_status=413)
        created_record = self.store.record_routed_request(
            workspace_id=identity.workspace_id,
            request_id=request.request_id,
            principal_id=identity.principal_id,
            device_id=grant.device_id,
            project_handle=project_handle,
            operation_id=operation_id,
            tool=tool,
            request_digest=request.arguments_digest,
            state="QUEUED",
            at=utc_now(),
        )
        key = (identity.workspace_id, request.request_id)
        async with self._guard:
            completed = self._completed.get(key)
            if completed is not None:
                return self._public_response(completed, replay=True)
            pending = self._pending.get(key)
            if pending is None:
                if self._inflight_by_device[grant.device_id] >= self.limits.concurrent_requests_per_device:
                    raise RemoteError(
                        RemoteErrorCode.RATE_LIMITED,
                        "The paired device has reached its concurrent operation limit",
                        retryable=True,
                        http_status=429,
                    )
                pending = _Pending(request, asyncio.get_running_loop().create_future())
                self._pending[key] = pending
                self._inflight_by_device[grant.device_id] += 1
                await self._queues[grant.device_id].put(request)
            elif pending.request.arguments_digest != request.arguments_digest:
                raise RemoteError(RemoteErrorCode.OPERATION_CONFLICT, "Remote replay payload conflicts", http_status=409)
        self.store.audit(
            "tool_requested", outcome="QUEUED", request_id=request.request_id,
            operation_id=operation_id, principal_id=identity.principal_id,
            workspace_id=identity.workspace_id, device_id=grant.device_id,
            project_handle=project_handle, capability=request.capability,
            actor=identity.principal_id, origin=request.origin,
            detail={"tool": tool, "replay": not created_record},
        )
        self.store.audit(
            "operation_queued", outcome="QUEUED", request_id=request.request_id,
            operation_id=operation_id, principal_id=identity.principal_id,
            workspace_id=identity.workspace_id, device_id=grant.device_id,
            project_handle=project_handle, capability=request.capability,
            actor=identity.principal_id, origin=request.origin,
        )
        try:
            response = await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout_seconds or self.limits.operation_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            async with self._guard:
                current = self._pending.get(key)
                if current is pending:
                    self._pending.pop(key, None)
                    self._inflight_by_device[grant.device_id] = max(
                        0, self._inflight_by_device[grant.device_id] - 1,
                    )
            self.store.update_routed_state(
                identity.workspace_id, request.request_id, "TIMED_OUT", utc_now(),
            )
            self.store.audit(
                "operation_failed", outcome=RemoteErrorCode.OPERATION_TIMEOUT.value,
                request_id=request.request_id, operation_id=operation_id,
                principal_id=identity.principal_id,
                workspace_id=identity.workspace_id, device_id=grant.device_id,
                project_handle=project_handle, capability=request.capability,
                actor=identity.principal_id, origin=request.origin,
            )
            raise RemoteError(
                RemoteErrorCode.OPERATION_TIMEOUT,
                "The local CW agent did not return before the remote deadline",
                retryable=True,
                http_status=504,
                details={"operation_id": operation_id},
            ) from exc
        return self._public_response(response, replay=not created_record)

    async def dispatch_https_read_only(
        self, identity: RemoteIdentity, *, project_handle: str, tool: str,
        arguments: Mapping[str, Any], request_id: str | None = None,
        operation_id: str | None = None, timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Dispatch only the fixed public HTTPS read profile.

        This check is deliberately below MCP discovery so an unlisted direct
        call, alias, or manipulated tool name cannot reach the general router.
        """
        https_read_only_tool_contract(tool)
        return await self.dispatch(
            identity,
            project_handle=project_handle,
            tool=tool,
            arguments=arguments,
            request_id=request_id,
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )

    async def accept_response(self, device_id: str, response: RemoteResponse) -> None:
        response.validate()
        grant = self.store.project_grant(response.project_handle)
        if grant is None or grant.device_id != device_id or grant.revoked_at is not None:
            raise RemoteError(RemoteErrorCode.PROJECT_SCOPE_VIOLATION, "Response project scope is invalid", http_status=403)
        key = (grant.workspace_id, response.request_id)
        async with self._guard:
            pending = self._pending.get(key)
            if pending is None:
                completed = self._completed.get(key)
                if completed == response:
                    self.store.audit(
                        "replay_detected", outcome="IDEMPOTENT", request_id=response.request_id,
                        operation_id=response.operation_id, device_id=device_id,
                        workspace_id=grant.workspace_id, project_handle=response.project_handle,
                    )
                    return
                raise RemoteError(RemoteErrorCode.OPERATION_CONFLICT, "Response has no matching request", http_status=409)
            request = pending.request
            if (
                request.device_id != device_id
                or request.project_handle != response.project_handle
                or request.operation_id != response.operation_id
            ):
                raise RemoteError(RemoteErrorCode.OPERATION_CONFLICT, "Response identity conflicts with request", http_status=409)
            self._completed[key] = response
            self._completed.move_to_end(key)
            while len(self._completed) > self.limits.completed_response_cache_size:
                self._completed.popitem(last=False)
            self._pending.pop(key, None)
            self._inflight_by_device[device_id] = max(0, self._inflight_by_device[device_id] - 1)
            if not pending.future.done():
                pending.future.set_result(response)
        self.store.update_routed_state(grant.workspace_id, response.request_id, response.status, utc_now())
        event = (
            "operation_cancelled" if response.status == "CANCELLED"
            else "operation_completed" if response.error is None
            else "operation_failed"
        )
        self.store.audit(
            event, outcome=response.status, request_id=response.request_id,
            operation_id=response.operation_id, device_id=device_id,
            workspace_id=grant.workspace_id, principal_id=grant.principal_id,
            project_handle=response.project_handle,
        )

    @staticmethod
    def _public_response(response: RemoteResponse, *, replay: bool) -> dict[str, Any]:
        if response.error is not None:
            return {
                "schema_version": 1,
                "operation_id": response.operation_id,
                "project_id": response.project_handle,
                "status": response.status,
                "idempotent_replay": replay,
                "error": response.error,
            }
        result = dict(response.result or {})
        result["project_id"] = response.project_handle
        result["idempotent_replay"] = bool(result.get("idempotent_replay")) or replay
        return result


class GatewayService:
    def __init__(self, store: RemoteStore, verifier: OAuthTokenVerifier, *, limits: GatewayLimits | None = None) -> None:
        self.store = store
        self.verifier = verifier
        self.pairing = PairingService(store)
        self.router = RemoteRouter(store, verifier, limits=limits)

    def create_project_grant(
        self, *, device_id: str, display_name: str, project_handle: str | None = None,
    ) -> ProjectGrantRecord:
        device = self.store.device(device_id)
        if device is None:
            raise RemoteError(RemoteErrorCode.DEVICE_NOT_PAIRED, "Device is not paired", http_status=401)
        if device.revoked_at is not None:
            raise RemoteError(RemoteErrorCode.DEVICE_REVOKED, "Device is revoked", http_status=403)
        handle = project_handle or "cwp_" + __import__("secrets").token_urlsafe(24)
        grant = self.store.create_project_grant(
            project_handle=handle,
            principal_id=device.principal_id,
            workspace_id=device.workspace_id,
            device_id=device.device_id,
            display_name=display_name,
            created_at=utc_now(),
        )
        self.store.audit(
            "project_grant_created", outcome="ALLOWED",
            principal_id=device.principal_id, workspace_id=device.workspace_id,
            device_id=device.device_id, project_handle=handle,
        )
        return grant

    def revoke_project_grant(self, project_handle: str) -> None:
        record = self.store.project_grant(project_handle)
        if record is None:
            raise RemoteError(RemoteErrorCode.PROJECT_NOT_GRANTED, "Project grant does not exist", http_status=404)
        self.store.revoke_project_grant(project_handle, utc_now())
        self.store.audit(
            "project_grant_revoked", outcome="REVOKED",
            principal_id=record.principal_id, workspace_id=record.workspace_id,
            device_id=record.device_id, project_handle=record.project_handle,
        )

    def revoke_device(self, device_id: str) -> None:
        record = self.store.device(device_id)
        if record is None:
            raise RemoteError(RemoteErrorCode.DEVICE_NOT_PAIRED, "Device is not paired", http_status=404)
        self.store.revoke_device(device_id, utc_now())
        self.store.audit(
            "device_revoked", outcome="REVOKED", device_id=device_id,
            principal_id=record.principal_id, workspace_id=record.workspace_id,
        )
