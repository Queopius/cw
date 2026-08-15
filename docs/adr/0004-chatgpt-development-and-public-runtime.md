# ADR 0004: use Secure MCP Tunnel for development; require an authenticated relay for public ChatGPT

## Status

Accepted for CW 0.11 development architecture. Public implementation deferred.

## Context

CW owns local repositories and `.cw` evidence. Current ChatGPT developer apps
can reach a private stdio MCP server through Secure MCP Tunnel, while public
plugin submission requires a stable public HTTPS MCP endpoint. Secure MCP
Tunnel is explicitly a development/private connection mechanism, not the
submission endpoint.

## Development decision

Use the official tunnel to forward only the existing `cw mcp chatgpt-dev`
stdio command. Do not add an HTTP listener, arbitrary tunnel target, cloud
state database, or new workflow capability. Require startup project grants,
fix origin as `chatgpt_app`, and advertise only the profile enabled for the
tested workspace.

## Public architecture assessment

| Option | Privacy/security | UX/reliability | Decision |
| --- | --- | --- | --- |
| Secure user-run tunnel | Excellent local ownership; OpenAI development permissions control reach | Appropriate for development/private use; not a public submission endpoint | Keep for development |
| User-run public HTTPS runtime | Local ownership, but every user must secure ingress, certificates, OAuth, updates, and uptime | High setup and support burden | Not the default |
| Authenticated relay to a user-run local agent | Stable public endpoint; local agent opens outbound connection; source and `.cw` remain local | Central auth/revocation plus reliable local pairing required | Smallest viable public architecture |
| Managed CW service | Simplest always-on endpoint but risks source/state upload, account dependency, and largest operational scope | Highest cost and privacy change | Defer unless demand proves necessary |

For a future public ChatGPT app, recommend a narrow authenticated relay. It
must relay only CW MCP envelopes to a paired, user-run local runtime; it must
not store source or become shell/filesystem/Git access.

## Future authentication contract

```text
ChatGPT identity
    → OAuth CW connection
    → revocable CW account/session
    → paired local device/runtime
    → explicit opaque project grant
    → typed chatgpt_app actor
    → CWApplication capability policy
```

Required properties are short-lived access tokens, rotation, refresh expiry,
connection and device revocation, least-privilege project grants, replay-safe
operation IDs, auditable actor/origin without unnecessary personal metadata,
multi-device separation, team/workspace grant policy, and a way to disable the
relay without changing `.cw` evidence. ChatGPT identity must never be treated
as local OS identity.

## Consequences

CW Core and `CWApplication` remain OpenAI-independent. Apps SDK UI stays
deferred because text/tool results are sufficient for acceptance. The next
public milestone is a remote bridge and authentication candidate, not
high-consequence MCP authorization.
