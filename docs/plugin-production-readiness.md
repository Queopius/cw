# Plugin production readiness

CW Plugin 0.1.0 retains the production-candidate contract introduced by the
Core 0.12 milestone. It does not deploy a production gateway or OAuth service,
and it is not a public submission.

## Official model verified

Reviewed **2026-08-15** against current official OpenAI documentation:

- [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins)
- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Authentication](https://developers.openai.com/plugins/build/auth)
- [Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [Submit plugins](https://developers.openai.com/plugins/deploy/submission)
- [Security and privacy](https://developers.openai.com/plugins/guides/security-privacy)
- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

The current package entry point is `.codex-plugin/plugin.json`. A plugin may
combine skills and MCP and does not require UI. A public MCP submission needs
a stable public streamable-HTTPS endpoint and domain verification. Secure MCP
Tunnel supports private/development access, not public distribution.
User-specific data and writes require OAuth 2.1 under the MCP authorization
contract, with scope enforcement on every call.

Official documentation says Developer Mode availability depends on account and
workspace policy; it does not define a stable guarantee that a plan label alone
enables writes. CW therefore treats discovered surface capability as evidence.

## Ten production decisions

1. **Deployment:** public HTTPS MCP gateway/relay plus paired outbound-only
   local CW agent ([ADR 0005](adr/0005-production-mcp-relay.md)).
2. **Authentication:** OAuth 2.1 authorization code with PKCE; CIMD preferred,
   DCR supported where the OpenAI client requires it.
3. **Project authorization:** explicit principal/workspace/device/project
   grants and opaque handles; no path arguments or repository enumeration.
4. **High consequence:** separate typed, action-bound, project-bound,
   evidence-bound, expiring, nonce-protected human authorization ceremony.
5. **ChatGPT Pro:** read-only default. Controlled actions only when discovery
   and current workspace policy actually expose them.
6. **Business/Enterprise:** admin opt-in plus least-privilege OAuth scopes;
   server policy is identical and high-consequence tools remain absent.
7. **Packaging:** keep the current skill + `.mcp.json` local package. Add
   `.app.json` only after a real remote connection is registered; never commit
   a development tunnel ID.
8. **Privacy:** source and `.cw` remain local. Only normalized, redacted CW
   envelopes cross the production relay by default.
9. **Operations:** Queopius owns gateway security, auth integration, routing,
   availability, and incident response; users own local-agent operation and
   project grants.
10. **Submission:** blocked until the gateway, OAuth, domain verification,
    legal/business material, and real production acceptance exist.

CW remains text/tool-first. No Apps SDK UI is included because normalized
results already support inspection and controlled workflows; a UI requires a
separate evidence-backed usability need.

## Capability policy

The accepted tool registry is unchanged. Reads, execution, and controlled
state mutations retain their existing schemas, project scope, operation IDs,
idempotency, locking, cancellation, independent review, and gate rules.
`PLATFORM_CAPABILITY_UNAVAILABLE`, `AUTHORIZATION_REQUIRED`,
`PROJECT_SCOPE_VIOLATION`, `OPERATION_CONFLICT`, and infrastructure failures
remain distinct.

OAuth permission is necessary but not sufficient: CWApplication still checks
workflow state and capability policy. `HIGH_CONSEQUENCE_AUTHORIZATION` is not a
normal OAuth scope and is not exposed in this candidate.

## Status

| Area | Status |
| --- | --- |
| Current plugin package and skill | READY |
| Deterministic local package/registry validation | READY |
| Production topology and trust boundary | DEFINED |
| OAuth and grant contract | DEFINED, NOT IMPLEMENTED |
| Public HTTPS gateway/relay | NOT IMPLEMENTED |
| Public domain verification | NOT RUN |
| Legal/business publication inputs | HUMAN INPUT REQUIRED |
| Public submission | NOT PERFORMED |

Therefore **production readiness is NOT READY** and **plugin submission is
BLOCKED**. The milestone is useful because the blockers are now explicit and
implementation can proceed without revisiting CW Core.
