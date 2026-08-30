# CW Plugin 0.1.0 finalization acceptance

Date: 2026-08-30

This is the current acceptance record for the independently versioned Plugin
candidate. It does not publish, tag, submit, deploy, or authorize a release.

## Identity

| Component | Accepted identity |
| --- | --- |
| CW Core / CLI | `0.18.3` |
| CW Plugin | `0.1.0` |
| Remote protocol | `cw.remote.v1` |
| Candidate filename | `cw-plugin-0.1.0.zip` |

The Plugin version is intentionally independent from Core. Compatibility is
`>=0.14.0,<1.0.0`, with `0.18.3` current-tested. The `v0.14.0` registry contains
the same 12 accepted tool contracts; subsequent Core changes strengthened
schema validation and added unexposed high-consequence capabilities rather
than changing the Plugin registry.

## Package and branding

The current OpenAI package entry point is `.codex-plugin/plugin.json`. The
manifest exposes `CW — Codex Workflow`, `Queopius | Fantomid LLC`, the canonical
square composer icon `./assets/cw-mark-64.png`, full canonical mark
`./assets/cw-mark.png`, and dark logo `./assets/cw-logo-dark.png`. Validator
checks prove that packaged images are PNG, paths remain inside the package, the
composer icon is square, each asset is below 1 MiB, and bytes match the
canonical repository assets.

The package contains local `.mcp.json` stdio wiring only. Current OpenAI docs
use `.app.json` for a real registered MCP connection mapping. No safe registered
connection technical ID exists in this repository, so no `.app.json`, tunnel
ID, client secret, or fictional connection is packaged. The public staging MCP
URL is entered separately in ChatGPT Developer Mode.

Packaged branding and an independently created Developer Mode connection are
separate. Refresh updates discovered MCP metadata; repository asset changes do
not by themselves update an existing manual connection. If its UI does not
offer icon editing, the connection may need to be recreated and the icon
selected/uploaded through that UI.

The current deterministic archive contains 12 files and has SHA-256
`62cc98683028f4143377cf4e5b795891ad15e5ac85b8997d2e6a93f61d8ed7e0`.
Two independent builds were byte-identical.

## Authoritative registry

| Classification | Count | Tools |
| --- | ---: | --- |
| `READ` | 7 | `cw_project_status`, `cw_project_inspect`, `cw_history`, `cw_explain`, `cw_completion_status`, `cw_gate_status`, `cw_operation_status` |
| `EXECUTION` | 2 | `cw_validate`, `cw_request_review` |
| `CONTROLLED_STATE_MUTATION` | 3 | `cw_phase_start`, `cw_retry`, `cw_operation_cancel` |
| `HIGH_CONSEQUENCE_AUTHORIZATION` | 0 | absent |

The local MCP and remote protocol registries derive from the same `TOOLS`
tuple. Human gate approval, extension authorization, release/deployment,
destructive repair, rebaseline, arbitrary shell/filesystem/Git, and generic
command execution remain absent.

## Live staging evidence

The following unauthenticated probes were run against
`https://staging-mcp.cwcli.dev`:

| Probe | Result |
| --- | --- |
| `/healthz` | `200`, Core `0.18.3`, Plugin `0.1.0`, protocol `cw.remote.v1`, dev SHA `64830ae85b1df4c751d62dc0c4b24b2e1f1a3fc0` |
| `/readyz` | `200`, `ready`, same identity |
| `/.well-known/oauth-protected-resource` | `200`, resource `https://staging-mcp.cwcli.dev/mcp`, issuer `https://login.cwcli.dev/`, ten narrow scopes |
| Auth0 OIDC metadata | `200`, exact issuer, registration endpoint, token method `none`, PKCE `S256` |
| Auth0 OAuth metadata | `200`, same discovery contract |
| `/mcp` GET/initialize without token | `401 AUTHENTICATION_REQUIRED` with protected-resource challenge |
| `/remote/pair` | `303` to `/remote/pair/login` |
| `/remote/pair/login` | starts OAuth with PKCE `S256`, resource/audience bound to staging MCP |
| OAuth continuation | `PASS`; Authorization Code + PKCE completes and the issuer-bound opaque subject constructs a CW principal |
| Pairing, grant, agent, read | `PASS`; one explicitly granted project is reachable through its opaque handle |

Auth0 advertises DCR through `registration_endpoint`. Its live discovery does
not advertise CIMD. No DCR client was created during this read-only audit.

## Pairing and ChatGPT acceptance

Local acceptance proves device request/approval/rejection/replay behavior,
explicit one-project grants, outbound agent routing, opaque handles, real
`CWApplication` reads, unknown/revoked/cross-project denial, prompt-injection
resistance, and absence of shell/filesystem/gate-approval tools.

Historical real ChatGPT + Secure MCP Tunnel read-only acceptance remains valid
for the private development path. Public-staging pairing, one-project grant,
outbound agent connection, and a real-project read now pass. Therefore current
public results are:

- required reads: `PASS` for the accepted staging project;
- unauthenticated/unknown access: `PASS` (fails closed);
- human gate approval, shell, arbitrary filesystem: `PASS` by registry absence
  and local negative acceptance; not presented as available tools;
- unauthorized/cross-project: `PASS`;
- controlled actions: `NOT TESTED` on the current public ChatGPT surface;
- real project: `PASS` in staging.

## Decision

**FUNCTIONAL PACKAGE READY — STAGING REAL PROJECT E2E PASS**

Production is not deployed and public publication is not authorized. The
Production EAP also remains operationally blocked until supported device and
individual grant revocation are available.

Do not infer production acceptance from staging. Promote the exact candidate
through governance, deploy the separate production service, and repeat the
pairing/grant/agent/read and negative matrix against production before opening
the EAP.
