"""Environment-driven deployment bootstrap for the hosting-neutral gateway.

This module belongs to the remote adapter.  It translates an external hosting
contract into existing gateway/auth/store objects and contains no CW workflow
policy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .auth import OAuthResourceConfig, OAuthTokenVerifier
from .gateway import GatewayLimits, GatewayService
from .persistence import RemoteStore
from .server import GatewayRuntimeIdentity, create_gateway_app, serve_gateway


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"Required staging environment variable is missing: {name}")
    return value


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    value = environment.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _positive_float(environment: Mapping[str, str], name: str, default: float) -> float:
    value = environment.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def _https(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an absolute HTTPS URL without embedded credentials")
    return value


@dataclass(frozen=True, slots=True)
class GatewayDeploymentConfig:
    environment: str
    build_sha: str
    host: str
    port: int
    database: Path
    oauth: OAuthResourceConfig
    allowed_hosts: tuple[str, ...]
    limits: GatewayLimits

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "GatewayDeploymentConfig":
        values = os.environ if environment is None else environment
        deployment_environment = _required(values, "CW_DEPLOYMENT_ENV")
        if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", deployment_environment) is None:
            raise ValueError("CW_DEPLOYMENT_ENV is invalid")
        build_sha = (values.get("CW_BUILD_SHA") or values.get("RENDER_GIT_COMMIT") or "").strip()
        if re.fullmatch(r"[0-9a-f]{40}", build_sha) is None:
            raise ValueError("CW_BUILD_SHA or RENDER_GIT_COMMIT must be a full lowercase Git SHA")
        resource = _https("CW_GATEWAY_RESOURCE_URL", _required(values, "CW_GATEWAY_RESOURCE_URL"))
        issuer = _https("CW_OAUTH_ISSUER_URL", _required(values, "CW_OAUTH_ISSUER_URL"))
        jwks = _https("CW_OAUTH_JWKS_URL", _required(values, "CW_OAUTH_JWKS_URL"))
        documentation = values.get("CW_GATEWAY_DOCUMENTATION_URL", "https://docs.cwcli.dev/remote-auth/").strip()
        _https("CW_GATEWAY_DOCUMENTATION_URL", documentation)
        database = Path(_required(values, "CW_GATEWAY_DATABASE"))
        if not database.is_absolute():
            raise ValueError("CW_GATEWAY_DATABASE must be an absolute path")
        hosts = tuple(
            item.strip() for item in values.get("CW_GATEWAY_ALLOWED_HOSTS", "").split(",") if item.strip()
        )
        if any("/" in item or "@" in item or len(item) > 253 for item in hosts):
            raise ValueError("CW_GATEWAY_ALLOWED_HOSTS contains an invalid host pattern")
        algorithms = tuple(
            item.strip() for item in values.get("CW_OAUTH_ALGORITHMS", "RS256").split(",") if item.strip()
        )
        oauth = OAuthResourceConfig(
            issuer=issuer,
            resource=resource,
            jwks_uri=jwks,
            workspace_claim=values.get("CW_OAUTH_WORKSPACE_CLAIM", "https://cwcli.dev/claims/workspace").strip(),
            algorithms=algorithms,
            documentation_url=documentation,
        )
        limits = GatewayLimits(
            requests_per_minute=_positive_int(values, "CW_LIMIT_REQUESTS_PER_MINUTE", 120),
            requests_per_device_per_minute=_positive_int(values, "CW_LIMIT_DEVICE_REQUESTS_PER_MINUTE", 240),
            pairing_requests_per_minute=_positive_int(values, "CW_LIMIT_PAIRING_REQUESTS_PER_MINUTE", 20),
            concurrent_requests_per_device=_positive_int(values, "CW_LIMIT_CONCURRENT_PER_DEVICE", 4),
            maximum_request_bytes=_positive_int(values, "CW_LIMIT_REQUEST_BYTES", 64 * 1024),
            maximum_agent_message_bytes=_positive_int(values, "CW_LIMIT_AGENT_MESSAGE_BYTES", 512 * 1024),
            operation_timeout_seconds=_positive_float(values, "CW_LIMIT_OPERATION_TIMEOUT_SECONDS", 30.0),
            agent_idle_seconds=_positive_float(values, "CW_LIMIT_AGENT_IDLE_SECONDS", 45.0),
            completed_response_cache_size=_positive_int(values, "CW_LIMIT_COMPLETED_CACHE", 1024),
        )
        return cls(
            environment=deployment_environment,
            build_sha=build_sha,
            host=values.get("CW_GATEWAY_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_positive_int(values, "PORT", 10000),
            database=database,
            oauth=oauth,
            allowed_hosts=hosts,
            limits=limits,
        )

    def create_app(self) -> tuple[object, RemoteStore]:
        store = RemoteStore(self.database)
        try:
            verifier = OAuthTokenVerifier(self.oauth, store)
            service = GatewayService(store, verifier, limits=self.limits)
            app = create_gateway_app(
                service,
                self.oauth,
                runtime_identity=GatewayRuntimeIdentity(
                    environment=self.environment,
                    build_sha=self.build_sha,
                ),
                allowed_hosts=self.allowed_hosts,
            )
        except Exception:
            store.close()
            raise
        return app, store


def main() -> int:
    config = GatewayDeploymentConfig.from_environment()
    app, store = config.create_app()
    try:
        return serve_gateway(app, host=config.host, port=config.port)
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover - exercised through container startup
    raise SystemExit(main())
