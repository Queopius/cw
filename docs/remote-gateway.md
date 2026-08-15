# CW Remote gateway

CW 0.13 implements a hosting-neutral, production-oriented gateway candidate.
It is not a hosted CW service and has not been submitted to OpenAI.

```text
ChatGPT / Codex
        | OAuth 2.1 + Streamable HTTP MCP
        v
CW Remote gateway
        | typed cw.remote.v1 messages
        v
outbound-only local CW agent
        |
        v
CWApplication -> CW Engine -> local repository + .cw
```

The gateway owns public protocol termination, token validation, scope checks,
tenant routing, project-grant lookup, availability, limits, correlation, and
minimum audit metadata. It never decides phase eligibility, creates a gate,
runs validation, reads a repository, or stores CW workflow truth.

## Endpoints

- `/mcp`: MCP Streamable HTTP endpoint.
- `/healthz`: process liveness.
- `/readyz`: store/router readiness.
- `/.well-known/oauth-protected-resource`: RFC 9728 metadata.
- `/remote/v1/pairing/*`: device pairing ceremony.
- `/remote/v1/agent/*`: signed agent poll, result, and grant endpoints.

The MCP tool registry is mechanically derived from the accepted local MCP
registry. There is no generic JSON-RPC proxy. The six read tools and six
controlled tools are the entire surface; high-consequence authorization is
absent.

The ASGI service can run behind any standards-compliant HTTPS terminator. The
CLI loopback/plain-HTTP allowance exists only for deterministic development.
No production host, domain, or provider is selected in 0.13.

Remote failures remain distinct: `AUTHENTICATION_REQUIRED`, `TOKEN_INVALID`,
`TOKEN_EXPIRED`, `SCOPE_REQUIRED`, `DEVICE_NOT_PAIRED`, `DEVICE_REVOKED`,
`AGENT_OFFLINE`, `PROJECT_NOT_GRANTED`, `PROJECT_SCOPE_VIOLATION`,
`OPERATION_CONFLICT`, `OPERATION_TIMEOUT`, `REMOTE_TRANSPORT_UNAVAILABLE`,
`PROTOCOL_VERSION_UNSUPPORTED`, `RATE_LIMITED`, and `REQUEST_TOO_LARGE`.
Availability is never reported as a workflow failure, and stack traces are not
normal MCP results.

See [remote authentication](remote-auth.md), [operations](remote-operations.md),
and the [0.13 acceptance record](acceptance/remote-gateway-0.13.md).
