"""Governed MCP adapter boundary.

This package deliberately imports no MCP SDK. The optional protocol binding is
loaded only by :mod:`cw.adapters.mcp.server` when the stdio runtime starts.
"""

from .chatgpt import ChatGPTSurface, chatgpt_development_config
from .runtime import MCPReadOnlyRuntime, MCPRuntime, RuntimeConfig

__all__ = [
    "ChatGPTSurface", "MCPReadOnlyRuntime", "MCPRuntime", "RuntimeConfig",
    "chatgpt_development_config",
]
