from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .errors import CwError, ErrorCode


class ActorOrigin(str, Enum):
    HUMAN_CLI = "human_cli"
    CHATGPT_APP = "chatgpt_app"
    CODEX_PLUGIN = "codex_plugin"
    MCP_CLIENT = "mcp_client"
    AUTOMATED_CI = "automated_ci"
    INTERNAL_SUPERVISOR = "internal_supervisor"
    PLANNER = "planner"
    REVIEWER = "reviewer"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    origin: ActorOrigin
    explicit_user_intent: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", self.actor_id):
            raise CwError("Actor identity is invalid", ErrorCode.AUTHORIZATION_REQUIRED)


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    action: str
    resource_id: str
    operation_id: str
    actor: Actor
    issued_at: str
    expires_at: str
    nonce: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "action": self.action,
            "resource_id": self.resource_id,
            "operation_id": self.operation_id,
            "actor_id": self.actor.actor_id,
            "actor_origin": self.actor.origin.value,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "authorization_nonce": self.nonce,
        }


@dataclass(frozen=True, slots=True)
class OperationContext:
    operation_id: str
    actor: Actor
    requested_capability: str
    authorization: AuthorizationGrant | None = None


_HUMAN_AUTHORIZATION_ORIGINS = {
    ActorOrigin.HUMAN_CLI,
    ActorOrigin.CHATGPT_APP,
    ActorOrigin.CODEX_PLUGIN,
}

_OPERATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def validate_operation_id(value: str) -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise CwError("Operation identity is invalid", ErrorCode.OPERATION_CONFLICT)
    return value


def issue_user_authorization(
    *, action: str, resource_id: str, operation_id: str, actor: Actor,
    lifetime_seconds: int = 300,
) -> AuthorizationGrant:
    if actor.origin not in _HUMAN_AUTHORIZATION_ORIGINS or not actor.explicit_user_intent:
        raise CwError(
            "Explicit human authorization is required",
            ErrorCode.AUTHORIZATION_REQUIRED,
            "Ask the operator to confirm this exact action.",
        )
    if not action or not resource_id or lifetime_seconds <= 0:
        raise CwError("Authorization scope is invalid", ErrorCode.AUTHORIZATION_REQUIRED)
    validate_operation_id(operation_id)
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    return AuthorizationGrant(
        action=action,
        resource_id=resource_id,
        operation_id=operation_id,
        actor=actor,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=(issued + timedelta(seconds=lifetime_seconds)).isoformat().replace("+00:00", "Z"),
        nonce=uuid.uuid4().hex,
    )


def validate_authorization(
    grant: AuthorizationGrant | None, *, action: str, resource_id: str,
) -> AuthorizationGrant:
    if grant is None:
        raise CwError("Explicit human authorization is required", ErrorCode.AUTHORIZATION_REQUIRED)
    if grant.action != action or grant.resource_id != resource_id:
        raise CwError("Authorization does not match this action", ErrorCode.AUTHORIZATION_REQUIRED)
    validate_operation_id(grant.operation_id)
    if grant.actor.origin not in _HUMAN_AUTHORIZATION_ORIGINS or not grant.actor.explicit_user_intent:
        raise CwError("Internal agents cannot authorize this action", ErrorCode.AUTHORIZATION_REQUIRED)
    try:
        expires = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CwError("Authorization expiry is invalid", ErrorCode.AUTHORIZATION_REQUIRED) from exc
    if expires <= datetime.now(timezone.utc):
        raise CwError("Authorization has expired", ErrorCode.AUTHORIZATION_REQUIRED)
    return grant
