# CW Remote gateway

The current `cw.remote.v1` implementation is a hosting-neutral,
production-oriented gateway candidate introduced in Core 0.13. Core 0.14 adds
a Render/Auth0 staging deployment contract, but the service is
not considered deployed until external evidence exists and has not been
submitted to OpenAI.

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
agent, pairing, and project-grant clients parse the gateway as an origin,
reject userinfo, paths, queries, fragments, deceptive hostname suffixes, and
invalid ports, and never follow redirects. Plain HTTP is limited to the exact
IP literals `127.0.0.1` and bracketed `::1`; `localhost`, trailing-dot names,
other `127/8` addresses, and non-loopback targets require HTTPS and are not
treated as loopback identities.
No production host, domain, or provider is selected by the current candidate;
the original non-deployment decision was recorded in the Core 0.13 milestone.

Remote failures remain distinct: `AUTHENTICATION_REQUIRED`, `TOKEN_INVALID`,
`TOKEN_EXPIRED`, `SCOPE_REQUIRED`, `DEVICE_NOT_PAIRED`, `DEVICE_REVOKED`,
`AGENT_OFFLINE`, `PROJECT_NOT_GRANTED`, `PROJECT_SCOPE_VIOLATION`,
`OPERATION_CONFLICT`, `OPERATION_TIMEOUT`, `REMOTE_TRANSPORT_UNAVAILABLE`,
`PROTOCOL_VERSION_UNSUPPORTED`, `RATE_LIMITED`, and `REQUEST_TOO_LARGE`.
Availability is never reported as a workflow failure, and stack traces are not
normal MCP results.

See [remote authentication](remote-auth.md), [operations](remote-operations.md),
the [staging environment contract](staging-environment.md), and the
[0.13 acceptance record](acceptance/remote-gateway-0.13.md).
