from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from cw import __version__

from .layout import DEFAULT_WIDTH, bounded_width, visible_ljust, wrap
from .theme import MARKER_COLORS, STATE_COLORS


@dataclass(slots=True)
class Console:
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    no_color: bool = False
    quiet: bool = False
    width_override: int | None = None
    color: bool = field(init=False)

    def __post_init__(self) -> None:
        self.color = bool(getattr(self.stream, "isatty", lambda: False)()) and not self.no_color and "NO_COLOR" not in os.environ

    def style(self, value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def line(self, value: str = "") -> None:
        if not self.quiet:
            # Live execution checkpoints must be visible immediately even when
            # stdout is redirected to CI logs instead of attached to a TTY.
            print(value, file=self.stream, flush=True)

    @property
    def width(self) -> int:
        detected = self.width_override or shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
        return bounded_width(detected)

    @property
    def inner_width(self) -> int:
        return max(16, self.width - 4)

    def header(self, title: str = "Codex Workflow", *, version: bool = False, branded: bool | None = None) -> None:
        if branded is None:
            branded = title == "Codex Workflow"
        inner = self.width - 4
        right = f"v{__version__}" if version else ""
        first = visible_ljust(f"CW · {title}", right, inner)
        self.line(self.style("╭" + "─" * (self.width - 2) + "╮", "2"))
        self.line(f"{self.style('│', '2')} {self.style(first, '1')} {self.style('│', '2')}")
        if branded:
            second = "by Queopius".ljust(inner)
            self.line(f"{self.style('│', '2')} {self.style(second, '2')} {self.style('│', '2')}")
        self.line(self.style("╰" + "─" * (self.width - 2) + "╯", "2"))
        self.line()

    def rule(self, *, indent: int = 2) -> None:
        self.line(" " * indent + self.style("─" * max(4, self.width - indent), "2"))

    def subsection(self, title: str) -> None:
        self.line("  " + self.style(title.upper(), "1"))

    def aligned(
        self,
        left: str,
        right: str,
        *,
        indent: int = 2,
        left_style: str | None = None,
        right_style: str | None = None,
    ) -> None:
        available = max(8, self.width - indent)
        if len(left) + len(right) + 1 > available:
            for item in wrap(left, available):
                self.line(" " * indent + (self.style(item, left_style) if left_style else item))
            self.line(" " * indent + max(0, available - len(right)) * " " + (self.style(right, right_style) if right_style else right))
            return
        gap = max(1, available - len(left) - len(right))
        rendered_left = self.style(left, left_style) if left_style else left
        rendered_right = self.style(right, right_style) if right_style else right
        self.line(" " * indent + rendered_left + " " * gap + rendered_right)

    def item(self, marker: str, message: str) -> None:
        self.line(f"{self.style(marker, MARKER_COLORS.get(marker, '0'))} {message}")

    def field(self, label: str, value: Any, width: int = 11) -> None:
        prefix = f"  {label.ljust(width)} "
        rendered = wrap(str(value), max(8, self.width - len(prefix)))
        self.line(f"  {self.style(label.ljust(width), '1')} {self.state(str(rendered[0]))}")
        for continuation in rendered[1:]:
            self.line(" " * len(prefix) + continuation)

    def state(self, value: str) -> str:
        code = STATE_COLORS.get(value)
        return self.style(value, code) if code else value

    def section(self, title: str) -> None:
        self.line(self.style(title, "1"))

    def phase(self, marker: str, number: str, name: str, *, indent: int = 2) -> None:
        prefix = " " * indent + f"{marker} {number:>2}  "
        lines = wrap(name, max(8, self.width - len(prefix)))
        colored = self.style(marker, MARKER_COLORS.get(marker, "0"))
        self.line(" " * indent + f"{colored} {number:>2}  {lines[0]}")
        for continuation in lines[1:]:
            self.line(" " * len(prefix) + continuation)

    def focus(self, marker: str, number: str, name: str, *, indent: int = 0) -> None:
        prefix = " " * indent + f"{marker} {number} · "
        lines = wrap(name, max(8, self.width - len(prefix)))
        colored = self.style(marker, MARKER_COLORS.get(marker, "0"))
        self.line(" " * indent + f"{colored} {self.style(f'{number} · {lines[0]}', '1')}")
        for continuation in lines[1:]:
            self.line(" " * len(prefix) + self.style(continuation, "1"))

    def wrapped(self, value: str, indent: int = 2) -> None:
        for line in wrap(value, self.width - indent):
            self.line(" " * indent + line)

    def action(self, command: str, description: str) -> None:
        command_width = min(18, max(10, self.width // 3))
        prefix = f"  {self.style(command.ljust(command_width), '36')}"
        lines = wrap(description, max(8, self.width - command_width - 2))
        self.line(prefix + lines[0])
        for continuation in lines[1:]:
            self.line(" " * (command_width + 2) + continuation)

    def run(self, command: str) -> None:
        self.line()
        self.line("  Run:")
        self.line(f"    {command}")


def emit_json(payload: Any, stream: TextIO | None = None) -> None:
    # One object per line keeps ordinary JSON machine-readable and allows
    # streaming commands to use the same function as a JSONL transport.
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=stream or sys.stdout,
        flush=True,
    )


def error_summary(code: str, message: str) -> tuple[str, str]:
    mapping = {
        "REVIEWER_NETWORK_ERROR": ("Reviewer unavailable", "Could not contact the Codex reviewer."),
        "REVIEW_TIMEOUT": ("Reviewer timed out", "The independent review exceeded its configured timeout."),
        "IMPLEMENTER_PROCESS_ERROR": ("Implementer stopped unexpectedly", "Codex did not finish the implementation session normally."),
        "EXECUTION_INTERRUPTED": ("Stop requested", "CW stopped the managed Codex operation and preserved workflow progress."),
        "PLANNER_NETWORK_ERROR": ("Planner unavailable", "Could not contact the Codex planner."),
        "PLANNER_TRANSPORT_ERROR": ("Planner transport failed", "The Codex planner connection closed before completion."),
        "PLANNER_SCHEMA_ERROR": ("Planner schema incompatible", "CW could not start planning because its structured output schema was rejected by Codex."),
        "PLANNER_PROCESS_ERROR": ("Planner failed", "Codex did not return a valid, safe workflow plan."),
        "PLAN_TIMEOUT": ("Planner timed out", "Repository planning exceeded its configured timeout."),
        "STALE_WORKFLOW_SHA": ("Plan proposal changed", "The amendment was prepared against another workflow SHA-256."),
        "COMPLETION_CONTRACT_CHANGE_REQUIRES_REBUILD": ("Completion Contract change rejected", "Plan amendments cannot change the Completion Contract."),
        "PLAN_AMEND_INTEGRITY_ERROR": ("Plan amendment failed", "The operation stopped and restored the previous proposal."),
        "PLAN_AMEND_ROLLBACK_FAILED": ("Plan amendment rollback failed", "Do not continue until the recorded backup has been restored."),
        "WORKFLOW_PROJECT_MISMATCH": ("Project workflow mismatch", "This workflow belongs to another repository."),
        "RUNTIME_NOT_WRITABLE": ("Runtime path is read-only", ".cw must be writable."),
        "INVALID_STATE": ("Workflow state invalid", message),
        "STATE_INCONSISTENT": ("Workflow state inconsistent", message),
        "INVALID_GATE": ("Approval gate invalid", message),
        "PROTECTED_PATH_MODIFIED": ("Protected workflow metadata changed", "CW detected an unauthorized metadata change and stopped safely."),
        "CODEX_NOT_FOUND": ("Codex not found", "The Codex CLI is required for planning and agent operations."),
        "CODEX_CONFIG_ERROR": ("Codex configuration invalid", "CW could not start Codex because its effective configuration was rejected."),
        "SCHEMA_VALIDATION_ERROR": ("Workflow data invalid", message),
        "SCHEMA_VERSION_ERROR": ("Workflow schema incompatible", message),
        "INTERNAL_ERROR": ("CW encountered an internal error", "The operation stopped safely. Run: cw error"),
        "PLAN_UNCLEAR": ("Project goal is unclear", message),
        "PLAN_REQUIRED": ("Development plan required", "Create a development plan before starting implementation."),
        "NOTHING_TO_VALIDATE": ("Nothing to validate", "Create a development plan first."),
        "UPDATE_CHECK_ERROR": ("Update check unavailable", "CW could not read release metadata. Normal workflow use is unaffected."),
        "UPDATE_DOWNLOAD_ERROR": ("Update download failed", "The existing CW installation remains active."),
        "UPDATE_CHECKSUM_ERROR": ("Update verification failed", "The downloaded package did not match its published SHA-256 checksum."),
        "UPDATE_SIGNATURE_ERROR": ("Release signature invalid", "CW refused to install an unverified release."),
        "UPDATE_MANIFEST_ERROR": ("Release metadata invalid", "CW rejected incompatible or untrusted release metadata."),
        "UPDATE_INSTALL_ERROR": ("Update installation failed", "The existing CW installation remains active and project data was not changed."),
        "UPDATE_SMOKE_TEST_ERROR": ("Updated CW failed verification", "The staged version did not pass its smoke test; the existing version remains active."),
        "UPDATE_ROLLBACK_ERROR": ("Rollback unavailable", "CW could not safely select a previous healthy installation."),
        "UPDATE_INCOMPATIBLE": ("Update incompatible", "The selected release cannot be installed safely."),
        "UPDATE_DEVELOPMENT_INSTALL": ("Development installation detected", "Self-update is disabled for editable/source installations."),
        "MCP_OPTIONAL_UNAVAILABLE": ("Optional integration unavailable", "The current phase is unaffected and may continue."),
        "MCP_REQUIRED_UNAVAILABLE": ("Required integration unavailable", "The current phase cannot continue safely."),
        "MCP_AUTH_REQUIRED": ("Integration authentication required", "A required MCP integration needs authorization."),
        "MCP_SERVER_ERROR": ("Integration server unavailable", "The MCP provider returned a server error."),
        "MCP_TRANSPORT_ERROR": ("Integration transport unavailable", "Codex could not initialize the MCP transport."),
        "MCP_DISABLED": ("Required integration disabled", "Enable the required MCP integration explicitly before continuing."),
        "MCP_NOT_CONFIGURED": ("Required integration not configured", "Configure the required MCP integration before continuing."),
        "BATCH_TOO_LARGE": ("Batch too large", "CW intentionally limits unattended execution."),
        "BATCH_TIME_EXHAUSTED": ("Batch time budget reached", "CW stopped the active agent safely and preserved workflow progress."),
        "BATCH_REVISION_EXHAUSTED": ("Semantic revision budget reached", "The current phase remains active and no next phase was started."),
        "BATCH_INTERRUPTED": ("Batch interrupted", "Completed gates and current workflow progress were preserved."),
        "LOCKED": ("Another CW operation is active", message),
    }
    return mapping.get(code, (message, "CW stopped safely."))
