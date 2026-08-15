# ADR 0002: next MCP transport

Status: accepted and validated by CW 0.8.

## Options

| Transport | Security and auth | Local repository access | Platform/UX |
| --- | --- | --- | --- |
| Local stdio | Process launch and configured roots define the trust boundary; no listening port | Direct and private | Simple lifecycle; feasible on Linux, Windows, and macOS; suited to local Codex |
| Localhost HTTP | Needs origin protection, authentication, port discovery, and robust daemon lifecycle | Direct | Cross-platform but a remote ChatGPT host cannot reach localhost; more exposed than stdio |
| Hosted streamable HTTP | OAuth 2.1 and stable HTTPS match the official public plugin model | Requires a secure user-controlled bridge or source upload | Best ChatGPT reach, highest privacy/auth/operations complexity |

## Decision

CW 0.8 implements a transport-neutral read-only handler and one local stdio
server. Implementation evidence validated command-started lifecycle, malformed
input isolation, EOF shutdown, machine-only stdout, stderr diagnostics, and
Linux/Windows/macOS-feasible subprocess behavior. Project roots remain local and
no listening port or network authentication surface is introduced.

Do not treat stdio as the final ChatGPT web transport. Official OpenAI
documentation distinguishes local Codex stdio configuration from hosted
ChatGPT plugin tools. A later remote milestone must design stable HTTPS
streamable HTTP, OAuth/authorization, an authenticated user-controlled runtime
bridge, and explicit privacy boundaries before exposing local repositories.

Localhost HTTP and hosted MCP remain deferred. The MCP SDK and protocol binding
live under `cw.adapters.mcp`; `cw.core` and `cw.application` import neither. A
future transport can reuse the handler without changing workflow semantics.
