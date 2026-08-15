# CW Remote authentication

CW 0.13 is an OAuth 2.1 protected-resource implementation, not an identity
provider. Production deployments must use an established standards-compliant
authorization server through the narrow discovery/JWKS adapter.

The resource server publishes RFC 9728 protected-resource metadata and
validates every bearer token for signature, issuer, resource/audience,
expiration, revocation, workspace identity, and the tool's exact scope. A
missing or invalid token receives a `WWW-Authenticate` challenge and fails
closed. Tokens and authorization codes are never stored in `.cw`, URLs, logs,
plugin metadata, or operation records.

Supported authorization-server contracts require Authorization Code with PKCE
S256 plus either Client ID Metadata Documents (preferred when supported) or
Dynamic Client Registration. Refresh-token issuance, rotation, and revocation
remain the authorization server's responsibility; the CW resource server
enforces access-token expiration and token-ID revocation.

Scopes are deliberately narrow:

| Scope | Operation class |
|---|---|
| `project.read` | status, inspect, explain |
| `gate.read` | gate status |
| `history.read` | history |
| `completion.read` | completion status |
| `operation.read` | operation polling |
| `phase.start` | engine-authorized current phase start |
| `validation.execute` | configured validation only |
| `review.execute` | independent CW review request |
| `retry.execute` | engine-classified retry |
| `operation.cancel` | safe queued cancellation |

Scope possession is necessary, never sufficient. Tenant/device/project grants
and local CW policy are independently checked. There is no OAuth scope for
human gate approval, extension authorization, release/deploy authorization,
destructive repair, or rebaseline.

Official platform requirements were rechecked on 2026-08-15 against the
[plugin model](https://developers.openai.com/plugins/concepts/plugins),
[MCP server concepts](https://developers.openai.com/plugins/concepts/mcp-server),
[MCP server build guide](https://developers.openai.com/plugins/build/mcp-server),
[authentication guide](https://developers.openai.com/plugins/build/auth),
[ChatGPT connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt),
[submission guide](https://developers.openai.com/plugins/deploy/submission),
[app review guide](https://developers.openai.com/plugins/deploy/app-review), and
[Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
No material change invalidated the CW 0.12 decisions.
