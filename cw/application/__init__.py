"""Stable, UI-independent application interface for CW adapters."""

from cw.core.authorization import (
    Actor,
    ActorOrigin,
    AuthorizationGrant,
    OperationContext,
    issue_user_authorization,
)

from .facade import CWApplication
from .models import ApplicationError, ApplicationErrorCode, OperationResult, OperationStatus
from .projects import ProjectHandle, ProjectResolver, ResolvedProject

__all__ = [
    "Actor", "ActorOrigin", "ApplicationError", "ApplicationErrorCode",
    "AuthorizationGrant", "CWApplication", "OperationContext", "OperationResult", "OperationStatus",
    "ProjectHandle", "ProjectResolver", "ResolvedProject",
    "issue_user_authorization",
]
