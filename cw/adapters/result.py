from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cw.core.errors import ErrorCode
from cw.integrations.models import IntegrationDiagnostic


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    """Canonical result for every CW-managed Codex child process.

    The field order preserves the public ``CodexResult(payload, stderr, stdout)``
    construction used by older adapters while adding the process facts needed
    by implementer, planner, and reviewer alike.
    """

    structured_payload: dict[str, Any] | None
    stderr: str
    stdout: str = ""
    exit_code: int = 0
    integration_diagnostics: tuple[IntegrationDiagnostic, ...] = ()
    terminal_error: ErrorCode | None = None
    startup_profile: dict[str, int] | None = None
    session_id: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return self.structured_payload or {}

    @property
    def mcp_diagnostics(self) -> tuple[IntegrationDiagnostic, ...]:
        """Compatibility name retained for the v0.3 public adapter surface."""

        return self.integration_diagnostics


# Backwards-compatible import name.  This is an alias, not a second model.
CodexResult = CodexRunResult
