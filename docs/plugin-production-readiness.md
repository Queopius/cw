# Plugin production readiness

CW Plugin 0.1.0 retains the production-candidate contract introduced by the
Core 0.12 milestone. Local MCP stdio is implemented. A staging HTTPS MCP
gateway and OAuth discovery are implemented for testing at the current dev
baseline. Production MCP/OAuth are not deployed, OpenAI domain verification is
not complete, and no universal submission or public Plugin publication exists.

## Technical publisher identity

- **Legal publisher:** Fantomid LLC
- **Technology brand:** Queopius
- **Product:** CW — Codex Workflow
- **Contact identity:** Queopius | Fantomid LLC
- **Website:** <https://cwcli.dev>
- **Documentation:** <https://docs.cwcli.dev>

Queopius is a technology brand operated by Fantomid LLC,
a New Mexico limited liability company.

## Official model verified

Current-state wording rechecked **2026-08-21** against official OpenAI
documentation:

- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)
- [Authentication](https://developers.openai.com/plugins/build/auth)
- [Submit plugins](https://developers.openai.com/plugins/deploy/submission)
- [Security and privacy](https://developers.openai.com/plugins/guides/security-privacy)

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
9. **Operations:** Fantomid LLC operating under the Queopius brand would own
   production gateway security, auth integration, routing, availability, and
   incident response; users own local-agent operation and project grants.
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
| Local MCP stdio | IMPLEMENTED |
| Staging MCP HTTPS | IMPLEMENTED FOR TESTING |
| Staging OAuth/discovery | IMPLEMENTED FOR TESTING |
| Production MCP HTTPS | NOT DEPLOYED |
| Production OAuth | NOT DEPLOYED |
| OpenAI domain verification | NOT COMPLETED |
| Legal/business publication inputs | HUMAN INPUT REQUIRED |
| Universal submission | NOT CREATED |
| Public Plugin publication | NOT COMPLETED |

Therefore **production readiness is NOT READY** and **plugin submission is
BLOCKED**. The milestone is useful because the blockers are now explicit and
implementation can proceed without revisiting CW Core.

## Canonical URL status

Checked on 2026-08-21. This table records current behavior; it does not deploy
or reserve any external URL.

| Purpose | URL | Classification |
| --- | --- | --- |
| Product website | <https://cwcli.dev> | `LIVE_CANONICAL` |
| Technical documentation | <https://docs.cwcli.dev> | `LIVE_CANONICAL` |
| Plugin documentation | <https://docs.cwcli.dev/en/stable/plugin-app-candidate/> | `LIVE_LOCALIZED` |
| Technical support | <https://docs.cwcli.dev/en/stable/plugin-support/> | `LIVE_LOCALIZED` |
| Remote authentication docs | <https://docs.cwcli.dev/en/stable/remote-auth/> | `LIVE_LOCALIZED` |
| Staging MCP | `https://staging-mcp.cwcli.dev/mcp` | `STAGING_ONLY` |
| Production MCP | none | `MISSING` |
| Production OAuth | none | `MISSING` |
| Final Privacy Policy | none | `MISSING` |
| Terms of Use | none | `MISSING` |

The unlocalized Plugin leaf URLs currently return 404. Public metadata uses the
existing localized stable Plugin page until a reliable version-neutral redirect
is provided outside this repository. No draft privacy document is linked as a
final policy.

## Legal readiness checklist

**DRAFT — REQUIRES HUMAN AND LEGAL REVIEW**

This checklist is internal readiness material, not legal advice and not a
submission document. Human and legal review must define or approve:

- Privacy Policy;
- Terms of Use;
- retention;
- deletion;
- subprocessors;
- contractual jurisdiction;
- incident-response commitments;
- regional availability;
- legal, privacy, and support contact details.

No final legal policy is published or linked by Plugin 0.1.0.

## Version boundary

- Core: `0.15.1`
- Plugin: `0.1.0`
- Remote protocol: `cw.remote.v1`
- Proposed next Plugin version: `0.2.0`
- Proposed version status: **NOT AUTHORIZED**

The proposal reflects the immutable published `0.1.0` bytes, tightened schemas,
and the future change in remote composition. This document does not modify a
version file or authorize a release.
