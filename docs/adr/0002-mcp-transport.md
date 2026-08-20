# ADR 0002: next MCP transport

Status: accepted in CW 0.8; revalidated for controlled actions in CW 0.9 and
ChatGPT development in CW 0.11.

## Options

| Transport | Security and auth | Local repository access | Platform/UX |
| --- | --- | --- | --- |
| Local stdio | Process launch and configured roots define the trust boundary; no listening port | Direct and private | Simple lifecycle; feasible on Linux, Windows, and macOS; suited to local Codex |
| Localhost HTTP | Needs origin protection, authentication, port discovery, and robust daemon lifecycle | Direct | Cross-platform but a remote ChatGPT host cannot reach localhost; more exposed than stdio |
| Hosted streamable HTTP | OAuth 2.1 and stable HTTPS match the official public plugin model | Requires a secure user-controlled bridge or source upload | Best ChatGPT reach, highest privacy/auth/operations complexity |

## Decision

CW implements a transport-neutral governed handler and one local stdio server.
CW 0.9 retained the transport while adding asynchronous controlled operations;
implementation evidence validates command-started lifecycle, malformed
input isolation, EOF shutdown, machine-only stdout, stderr diagnostics, and
subprocess stdin isolation. Project roots remain local and no listening port or
network authentication surface is introduced.

Secure MCP Tunnel can now forward directly to a configured private stdio
server. CW 0.11 therefore reuses this adapter for ChatGPT development instead
of adding localhost HTTP. The tunnel is not a public submission endpoint. A
later public milestone still needs stable HTTPS, OAuth, an authenticated
user-controlled relay, and explicit privacy boundaries.

Localhost HTTP and hosted MCP remain deferred. The MCP SDK and protocol binding
live under `cw.adapters.mcp`; `cw.core` and `cw.application` import neither. A
future transport can reuse the handler without changing workflow semantics.
