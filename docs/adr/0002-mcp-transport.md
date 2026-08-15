# ADR 0002: next MCP transport

Status: recommendation for the next milestone; no transport is implemented in
CW 0.7.

## Options

| Transport | Security and auth | Local repository access | Platform/UX |
| --- | --- | --- | --- |
| Local stdio | Process launch and configured roots define the trust boundary; no listening port | Direct and private | Simple lifecycle; feasible on Linux, Windows, and macOS; suited to local Codex |
| Localhost HTTP | Needs origin protection, authentication, port discovery, and robust daemon lifecycle | Direct | Cross-platform but a remote ChatGPT host cannot reach localhost; more exposed than stdio |
| Hosted streamable HTTP | OAuth 2.1 and stable HTTPS match the official public plugin model | Requires a secure user-controlled bridge or source upload | Best ChatGPT reach, highest privacy/auth/operations complexity |

## Decision

Implement a transport-neutral handler and local stdio server first for the
read-only CW MCP Runtime milestone. Do not treat stdio as the final ChatGPT
transport. Official OpenAI documentation currently recommends stable HTTPS
streamable HTTP and MCP authorization for production plugin servers, so a later
ChatGPT milestone must design an authenticated user-controlled runtime bridge
before exposing local repositories.

Do not build localhost HTTP or hosted MCP in the next milestone. The handler
must keep transport, authentication, and OpenAI packages outside the engine so
another transport can be added without changing workflow semantics.

