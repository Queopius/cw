from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Any, TextIO

from .theme import MARKER_COLORS, STATE_COLORS


@dataclass(slots=True)
class Console:
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    no_color: bool = False
    quiet: bool = False
    color: bool = field(init=False)

    def __post_init__(self) -> None:
        self.color = bool(getattr(self.stream, "isatty", lambda: False)()) and not self.no_color and "NO_COLOR" not in os.environ

    def style(self, value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def line(self, value: str = "") -> None:
        if not self.quiet:
            print(value, file=self.stream)

    def header(self, title: str = "Codex Workflow") -> None:
        self.line(self.style(f"CW by Queopius · {title}", "1;36"))
        self.line()

    def item(self, marker: str, message: str) -> None:
        self.line(f"{self.style(marker, MARKER_COLORS.get(marker, '0'))} {message}")

    def field(self, label: str, value: Any, width: int = 11) -> None:
        prefix = f"  {label.ljust(width)} "
        terminal = min(100, max(80, shutil.get_terminal_size((96, 24)).columns))
        rendered = textwrap.wrap(str(value), width=max(20, terminal - len(prefix))) or [""]
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
        terminal = min(100, max(80, shutil.get_terminal_size((96, 24)).columns))
        lines = textwrap.wrap(name, width=max(20, terminal - len(prefix))) or [""]
        colored = self.style(marker, MARKER_COLORS.get(marker, "0"))
        self.line(" " * indent + f"{colored} {number:>2}  {lines[0]}")
        for continuation in lines[1:]:
            self.line(" " * len(prefix) + continuation)

    def wrapped(self, value: str, indent: int = 2) -> None:
        terminal = min(100, max(80, shutil.get_terminal_size((96, 24)).columns))
        for line in textwrap.wrap(value, width=terminal - indent) or [""]:
            self.line(" " * indent + line)

    def run(self, command: str) -> None:
        self.line()
        self.line("  Run:")
        self.line(f"    {command}")


def emit_json(payload: Any, stream: TextIO | None = None) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), file=stream or sys.stdout)


def error_summary(code: str, message: str) -> tuple[str, str]:
    mapping = {
        "REVIEWER_NETWORK_ERROR": ("Reviewer unavailable", "Could not contact the Codex reviewer."),
        "REVIEW_TIMEOUT": ("Reviewer timed out", "The independent review exceeded its configured timeout."),
        "IMPLEMENTER_PROCESS_ERROR": ("Implementer stopped unexpectedly", "Codex did not finish the implementation session normally."),
        "PLANNER_NETWORK_ERROR": ("Planner unavailable", "Could not contact the Codex planner."),
        "PLANNER_PROCESS_ERROR": ("Planner failed", "Codex did not return a valid, safe workflow plan."),
        "PLAN_TIMEOUT": ("Planner timed out", "Repository planning exceeded its configured timeout."),
        "WORKFLOW_PROJECT_MISMATCH": ("Project workflow mismatch", "This workflow belongs to another repository."),
        "RUNTIME_NOT_WRITABLE": ("Runtime path is read-only", ".cw must be writable."),
        "INVALID_STATE": ("Workflow state invalid", message),
        "INVALID_GATE": ("Approval gate invalid", message),
        "PROTECTED_PATH_MODIFIED": ("Protected workflow metadata changed", "CW detected an unauthorized metadata change and stopped safely."),
        "CODEX_NOT_FOUND": ("Codex not found", "The Codex CLI is required for planning and agent operations."),
        "SCHEMA_VALIDATION_ERROR": ("Workflow data invalid", message),
        "SCHEMA_VERSION_ERROR": ("Workflow schema incompatible", message),
        "INTERNAL_ERROR": ("CW encountered an internal error", "The operation stopped safely. Run: cw error"),
        "PLAN_UNCLEAR": ("Project goal is unclear", message),
        "LOCKED": ("Another CW operation is active", message),
    }
    return mapping.get(code, (message, "CW stopped safely."))


HELP = """CW by Queopius · Codex Workflow

Usage:
  cw [command]

Workflow
  init        Initialize CW
  plan        Create or inspect the plan
  start       Start or resume development
  status      Show current workflow status
  validate    Run deterministic validation
  review      Run independent review
  retry       Retry a failed operation
  history     Show workflow history

Maintenance
  doctor      Check environment and integration
  repair      Repair or migrate workflow metadata
  config      Show configuration
  error       Show last detailed error
  version     Show CW version
  help        Show help

Examples
  cw init
  cw plan
  cw
  cw status
"""
