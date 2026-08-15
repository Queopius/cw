from __future__ import annotations

import asyncio
import contextvars
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse, urlunparse

from .errors import RemoteError, RemoteErrorCode
from .persistence import RemoteStore
from .protocol import RemoteIdentity, all_remote_scopes


_identity_context: contextvars.ContextVar[RemoteIdentity | None] = contextvars.ContextVar(
    "cw_remote_identity", default=None,
)


def current_identity() -> RemoteIdentity:
    identity = _identity_context.get()
    if identity is None:
        raise RemoteError(
            RemoteErrorCode.AUTHENTICATION_REQUIRED,
            "OAuth authentication is required",
            http_status=401,
        )
    return identity


class JWKProvider(Protocol):
    def signing_key(self, token: str) -> Any:
        """Return a PyJWT-compatible verification key for a bearer token."""


class PyJWKSetProvider:
    """Production JWK provider backed by PyJWT's cached HTTPS JWK client."""

    def __init__(self, jwks_uri: str) -> None:
        parsed = urlparse(jwks_uri)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Production JWKS URI must be HTTPS")
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
        self._client = jwt.PyJWKClient(jwks_uri, cache_keys=True)

    def signing_key(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


class StaticJWKProvider:
    """Deterministic fixture provider; production configuration never uses it."""

    def __init__(self, keys: Mapping[str, Any]) -> None:
        self._keys = dict(keys)

    def signing_key(self, token: str) -> Any:
        try:
            import jwt
            header = jwt.get_unverified_header(token)
            return self._keys[str(header["kid"])]
        except (ImportError, KeyError, TypeError, ValueError) as exc:
            raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "Bearer token key is invalid", http_status=401) from exc


@dataclass(frozen=True, slots=True)
class OAuthResourceConfig:
    issuer: str
    resource: str
    jwks_uri: str
    workspace_claim: str = "cw_workspace"
    algorithms: tuple[str, ...] = ("RS256",)
    clock_skew_seconds: int = 30
    documentation_url: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("issuer", self.issuer), ("resource", self.resource), ("jwks_uri", self.jwks_uri)):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"OAuth {label} must be an absolute HTTPS URL")
        if self.resource.endswith("/"):
            raise ValueError("OAuth resource identifier must be canonical without a trailing slash")
        if not self.algorithms or any(item.lower() == "none" for item in self.algorithms):
            raise ValueError("OAuth token algorithms must be explicit asymmetric algorithms")


class OAuthTokenVerifier:
    """OAuth 2.1 resource-server JWT verifier.

    CW delegates authentication and authorization-code/PKCE/CIMD/DCR handling
    to an established identity provider.  This component performs only the
    resource-server duties: cryptographic signature, issuer, audience/resource,
    expiry, revocation, identity and per-request scope validation.
    """

    def __init__(
        self, config: OAuthResourceConfig, store: RemoteStore, *,
        keys: JWKProvider | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.keys = keys or PyJWKSetProvider(config.jwks_uri)

    def verify(self, token: str) -> RemoteIdentity:
        if not token or len(token) > 16384:
            raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "Bearer token is invalid", http_status=401)
        try:
            import jwt
            key = self.keys.signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self.config.algorithms),
                audience=self.config.resource,
                issuer=self.config.issuer,
                leeway=self.config.clock_skew_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except RemoteError:
            raise
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
        except Exception as exc:
            name = type(exc).__name__.lower()
            code = RemoteErrorCode.TOKEN_EXPIRED if "expired" in name else RemoteErrorCode.TOKEN_INVALID
            message = "Bearer token has expired" if code is RemoteErrorCode.TOKEN_EXPIRED else "Bearer token validation failed"
            self.store.audit("oauth_rejected", outcome=code.value)
            raise RemoteError(code, message, http_status=401) from exc
        subject = claims.get("sub")
        workspace = claims.get(self.config.workspace_claim)
        client_id = claims.get("client_id") or claims.get("azp") or "oauth-client"
        token_id = claims.get("jti")
        scope_value = claims.get("scope", "")
        if not all(isinstance(value, str) for value in (subject, workspace, client_id, scope_value)):
            raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "Bearer token identity claims are invalid", http_status=401)
        if token_id is not None and not isinstance(token_id, str):
            raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "Bearer token identifier is invalid", http_status=401)
        if token_id and self.store.token_revoked(self.config.issuer, token_id):
            raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "Bearer token has been revoked", http_status=401)
        identity = RemoteIdentity(
            principal_id=subject,
            workspace_id=workspace,
            client_id=client_id,
            scopes=frozenset(item for item in scope_value.split() if item),
            token_id=token_id,
        )
        self.store.audit(
            "oauth_authenticated", outcome="ALLOWED",
            principal_id=identity.principal_id, workspace_id=identity.workspace_id,
        )
        return identity

    def require_scope(self, identity: RemoteIdentity, scope: str) -> None:
        if scope not in identity.scopes:
            self.store.audit(
                "scope_violation", outcome="DENIED",
                principal_id=identity.principal_id, workspace_id=identity.workspace_id,
                capability=scope,
            )
            self.store.audit(
                "capability_denied", outcome="DENIED",
                principal_id=identity.principal_id, workspace_id=identity.workspace_id,
                capability=scope, actor=identity.principal_id, origin="remote_plugin",
            )
            raise RemoteError(
                RemoteErrorCode.SCOPE_REQUIRED,
                "The access token lacks the required CW scope",
                http_status=403,
                details={"required_scope": scope},
            )


@dataclass(frozen=True, slots=True)
class AuthorizationServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    code_challenge_methods_supported: tuple[str, ...]
    client_id_metadata_document_supported: bool
    registration_endpoint: str | None
    token_endpoint_auth_methods_supported: tuple[str, ...]

    @classmethod
    def validate(cls, payload: Mapping[str, Any], *, expected_issuer: str) -> "AuthorizationServerMetadata":
        issuer = payload.get("issuer")
        authorization = payload.get("authorization_endpoint")
        token = payload.get("token_endpoint")
        jwks = payload.get("jwks_uri")
        pkce = payload.get("code_challenge_methods_supported", [])
        methods = payload.get("token_endpoint_auth_methods_supported", [])
        registration = payload.get("registration_endpoint")
        cimd = payload.get("client_id_metadata_document_supported") is True
        if issuer != expected_issuer:
            raise RemoteError(RemoteErrorCode.TOKEN_INVALID, "Authorization-server issuer does not match configuration")
        urls = (authorization, token, jwks)
        if not all(isinstance(value, str) and urlparse(value).scheme == "https" for value in urls):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Authorization-server endpoints must be HTTPS")
        if not isinstance(pkce, list) or "S256" not in pkce:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Authorization server must advertise PKCE S256")
        if not cimd and not isinstance(registration, str):
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Authorization server must support CIMD or DCR")
        if not isinstance(methods, list) or not set(methods) & {"none", "private_key_jwt", "client_secret_basic", "client_secret_post"}:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Authorization server token authentication metadata is incomplete")
        return cls(
            issuer=issuer,
            authorization_endpoint=authorization,
            token_endpoint=token,
            jwks_uri=jwks,
            code_challenge_methods_supported=tuple(pkce),
            client_id_metadata_document_supported=cimd,
            registration_endpoint=registration if isinstance(registration, str) else None,
            token_endpoint_auth_methods_supported=tuple(str(item) for item in methods),
        )


async def discover_authorization_server(issuer: str, *, timeout_seconds: float = 5.0) -> AuthorizationServerMetadata:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
    parsed = urlparse(issuer)
    issuer_path = parsed.path.rstrip("/")
    endpoints = (
        urlunparse(parsed._replace(
            path="/.well-known/oauth-authorization-server" + issuer_path,
            params="", query="", fragment="",
        )),
        urlunparse(parsed._replace(
            path=issuer_path + "/.well-known/openid-configuration",
            params="", query="", fragment="",
        )),
    )
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        for endpoint in endpoints:
            response = await client.get(endpoint)
            if response.status_code == 200:
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Authorization-server metadata is not JSON") from exc
                if not isinstance(payload, dict):
                    raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Authorization-server metadata is invalid")
                return AuthorizationServerMetadata.validate(payload, expected_issuer=issuer)
    raise RemoteError(
        RemoteErrorCode.REMOTE_TRANSPORT_UNAVAILABLE,
        "Authorization-server discovery is unavailable",
        retryable=True,
        http_status=503,
    )


class OAuthResourceMiddleware:
    """ASGI bearer middleware with RFC 9728 discovery challenge."""

    def __init__(self, app: Any, verifier: OAuthTokenVerifier, *, public_paths: set[str] | None = None) -> None:
        self.app = app
        self.verifier = verifier
        self.public_paths = public_paths or set()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") in self.public_paths:
            await self.app(scope, receive, send)
            return
        if scope.get("path", "").startswith("/remote/v1/agent/"):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if not authorization.startswith("Bearer "):
            await self._reject(send, RemoteError(
                RemoteErrorCode.AUTHENTICATION_REQUIRED, "OAuth authentication is required", http_status=401,
            ))
            return
        try:
            identity = await asyncio.to_thread(self.verifier.verify, authorization[7:])
        except RemoteError as exc:
            await self._reject(send, exc)
            return
        token = _identity_context.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _identity_context.reset(token)

    async def _reject(self, send: Any, error: RemoteError) -> None:
        body = json.dumps({"error": error.to_dict()}, separators=(",", ":")).encode("utf-8")
        parsed = urlparse(self.verifier.config.resource)
        metadata = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource"
        challenge = f'Bearer resource_metadata="{metadata}"'
        await send({
            "type": "http.response.start",
            "status": error.http_status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", challenge.encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def protected_resource_metadata(config: OAuthResourceConfig) -> dict[str, Any]:
    return {
        "resource": config.resource,
        "authorization_servers": [config.issuer],
        "scopes_supported": list(all_remote_scopes()),
        "resource_documentation": config.documentation_url or config.resource + "/docs/remote-auth",
        "bearer_methods_supported": ["header"],
    }
