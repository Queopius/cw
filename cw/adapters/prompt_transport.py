from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cw.core.errors import CwError, ErrorCode


MAX_PROMPT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PromptTransport:
    """Bounded, exact UTF-8 prompt payload delivered through child stdin."""

    payload: bytes
    sha256: str

    @classmethod
    def create(cls, prompt: str, *, role: str) -> "PromptTransport":
        if not isinstance(prompt, str):
            raise TypeError("Codex prompt must be text")
        payload = prompt.encode("utf-8")
        if len(payload) > MAX_PROMPT_BYTES:
            code = (
                ErrorCode.PLANNER_TRANSPORT_ERROR
                if role.endswith("planner")
                else ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR
                if role.endswith("reviewer")
                else ErrorCode.IMPLEMENTER_PROCESS_ERROR
            )
            raise CwError(
                "Codex prompt exceeds the supported transport limit",
                code,
                "Reduce generated evidence or split the governed operation",
                details=(
                    f"stage=prompt_transport provider=codex mode=stdin "
                    f"bytes={len(payload)} maximum_bytes={MAX_PROMPT_BYTES} retry_safe=false"
                ),
            )
        return cls(payload=payload, sha256=hashlib.sha256(payload).hexdigest())
