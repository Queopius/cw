"""Read-only MCP adapter boundary.

This package deliberately imports no MCP SDK. The optional protocol binding is
loaded only by :mod:`cw.adapters.mcp.server` when the stdio runtime starts.
"""

from .runtime import MCPReadOnlyRuntime, RuntimeConfig

__all__ = ["MCPReadOnlyRuntime", "RuntimeConfig"]
