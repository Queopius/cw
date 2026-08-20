from __future__ import annotations

from enum import Enum
from pathlib import Path

from cw.application import Actor, ActorOrigin

from .runtime import RuntimeConfig, TOOLS


class ChatGPTSurface(str, Enum):
    """Capabilities the currently tested ChatGPT workspace permits."""

    READ_ONLY = "read-only"
    CONTROLLED_ACTIONS = "controlled-actions"


_READ_TOOL_NAMES = frozenset({
    "cw_project_status",
    "cw_project_inspect",
    "cw_history",
    "cw_explain",
    "cw_completion_status",
    "cw_gate_status",
})
_ACCEPTED_TOOL_NAMES = frozenset(item.name for item in TOOLS)


def chatgpt_development_config(
    project_paths: list[Path] | tuple[Path, ...],
    allowed_roots: list[Path] | tuple[Path, ...] | None = None,
    *,
    surface: ChatGPTSurface = ChatGPTSurface.READ_ONLY,
) -> RuntimeConfig:
    """Build a startup-only, explicitly granted ChatGPT development scope.

    The actor is fixed by this trusted adapter bootstrap. Remote callers cannot
    provide or elevate it through MCP tool arguments.
    """

    enabled = (
        _READ_TOOL_NAMES
        if surface is ChatGPTSurface.READ_ONLY
        else _ACCEPTED_TOOL_NAMES
    )
    return RuntimeConfig.create(
        project_paths,
        allowed_roots,
        actor=Actor("chatgpt-development", ActorOrigin.CHATGPT_APP),
        enabled_tools=enabled,
        surface=f"chatgpt-development:{surface.value}",
    )
