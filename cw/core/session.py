from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .models import Phase, Workflow
from .schema import SCHEMA_VERSION, schema_version
from .utils import atomic_json, load_json, utc_now


SESSION_FILE = ".cw/runtime/implementer-session.json"
READINESS_FILE = ".cw/runtime/READY_FOR_REVIEW.json"


def session_path(root: Path) -> Path:
    return root / SESSION_FILE


def readiness_path(root: Path) -> Path:
    return root / READINESS_FILE


def create_session(root: Path, workflow: Workflow, phase: Phase) -> dict[str, Any]:
    if readiness_path(root).exists():
        raise CwError(
            "A readiness manifest already exists",
            ErrorCode.INVALID_STATE,
            "Run: cw review",
            details=READINESS_FILE,
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "session_id": secrets.token_hex(16),
        "workflow": workflow.id,
        "phase": phase.id,
        "status": "ACTIVE",
        "started_at": utc_now(),
    }
    atomic_json(session_path(root), payload)
    return payload


def load_session(root: Path, workflow: Workflow, phase: Phase) -> dict[str, Any] | None:
    path = session_path(root)
    if not path.exists():
        return None
    if path.is_symlink():
        raise CwError("Implementer session metadata cannot be a symlink", ErrorCode.INVALID_STATE, "Run: cw repair")
    data = load_json(path)
    schema_version(data, "Implementer session")
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "session_id", "workflow", "phase", "status", "started_at"}
        or not isinstance(data.get("session_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", data["session_id"]) is None
        or data.get("workflow") != workflow.id
        or data.get("phase") != phase.id
        or data.get("status") != "ACTIVE"
        or not isinstance(data.get("started_at"), str)
    ):
        raise CwError("Implementer session metadata is invalid", ErrorCode.INVALID_STATE, "Run: cw repair")
    return data


def finish_session(root: Path) -> None:
    session_path(root).unlink(missing_ok=True)
