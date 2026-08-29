# CW — Codex Workflow Plugin

CW Plugin packages the `cw-workflow` skill with the existing scoped CW MCP
adapter. It uses CW Core, `CWApplication`, and the authoritative MCP registry;
it is not a second workflow engine.

## Identity and compatibility

- **Product:** CW — Codex Workflow
- **Legal publisher:** Fantomid LLC
- **Technology brand:** Queopius
- **Contact identity:** Queopius | Fantomid LLC
- **Core current and tested:** `0.18.3`
- **Plugin:** `0.1.0`
- **Supported Core:** `>=0.14.0,<1.0.0`
- **Remote protocol:** `cw.remote.v1` (strict)
- **Website:** <https://cwcli.dev>
- **Documentation:** <https://docs.cwcli.dev>
- **License:** Apache-2.0; the archive bundles `LICENSE` and `NOTICE`

Queopius is a technology brand operated by Fantomid LLC,
a New Mexico limited liability company. Plugin and Core versions are
independent: the Plugin archive is `cw-plugin-0.1.0.zip`, not a Core-versioned
filename.

The minimum remains Core `0.14.0`: that release exposes the same accepted
12-tool CWApplication/MCP registry required by Plugin `0.1.0`. Core `0.18.3`
is the current tested runtime. No compatibility beyond `<1.0.0` is claimed.

## Install for Codex evaluation

This repository marketplace is a development/evaluation source only. It is not
a workspace or universal public publication.

```text
codex plugin marketplace add <CW_REPOSITORY_ROOT>
codex plugin list --marketplace cw-development --available
codex plugin add cw@cw-development
```

The installed Core command must be compatible and the selected repository must
already be initialized by CW. The packaged `.mcp.json` starts only the existing
scoped local stdio adapter for the explicitly granted current project. A safely
verified ZIP can be expanded into a temporary marketplace with
`scripts/prepare_plugin_marketplace.py`; the Codex CLI does not install the ZIP
directly.

Remove the Plugin and marketplace source separately:

```text
codex plugin remove cw@cw-development
codex plugin marketplace remove cw-development
```

CLI `0.150.1` has no `plugin disable` command. Removal never authorizes deletion
of a project or `.cw` state.

## ChatGPT and public staging

The staging MCP resource is:

```text
https://staging-mcp.cwcli.dev/mcp
```

Its OAuth issuer is `https://login.cwcli.dev/`. ChatGPT Developer Mode accepts
the public `/mcp` URL as a separately created MCP connection. The current
OpenAI package schema uses `.app.json` only for a real registered MCP connection
mapping. This package does not contain `.app.json`: no connection technical ID
is committed, no tunnel ID is committed, and no OAuth client secret is needed
or embedded. Therefore installing these package files does not create or update
an existing manual ChatGPT connection.

As verified on 2026-08-29, staging health, readiness, protected-resource
metadata, Auth0 discovery, and PKCE `S256` are live on Core `0.18.3`. The human
pairing entry point `/remote/pair` starts OAuth but Auth0 currently rejects the
pairing browser client for the staging MCP audience. Until that external Auth0
resource-server assignment is corrected and acceptance is rerun, classification
is **FUNCTIONAL PACKAGE READY — REAL PROJECT E2E BLOCKED**.

| Distribution surface | Status |
| --- | --- |
| Local MCP stdio | `IMPLEMENTED` |
| Staging MCP HTTPS/OAuth discovery | `IMPLEMENTED_FOR_TESTING` |
| Staging device pairing / real project E2E | `BLOCKED` |
| Production MCP HTTPS | `NOT_DEPLOYED` |
| Production OAuth | `NOT_DEPLOYED` |
| Universal Plugin publication | `NOT_COMPLETED` |

After MCP metadata changes, a Developer Mode connection can be opened and
refreshed. Packaged icons are a separate install-surface branding path. The
current OpenAI documentation does not establish that Refresh applies packaged
branding to an independently created connection; if the connection UI does not
offer icon editing, recreate the development connection and upload/select the
icon there if that surface requests one.

## Pairing and project grants

Real project access requires all of the following:

1. `cw remote pair` creates a private device credential locally.
2. The human opens `/remote/pair`, signs in, and explicitly approves the exact
   device. OAuth login alone is not approval.
3. `cw remote grant` grants one initialized project and yields an opaque handle.
4. The outbound-only local agent connects and maps that handle to the one local
   CW project.
5. Remote reads reach the existing `CWApplication` through `cw.remote.v1`.

The gateway never accepts a caller-provided local path. Device pairing and each
project grant are independently revocable.

## Capabilities and authorization boundary

Plugin `0.1.0` exposes 12 tools from the authoritative registry:

- seven reads: status, inspect, history, explain, completion, gates, and
  operation status;
- two execution operations: configured validation and Program Review request;
- three controlled mutations: current-phase start, engine-classified retry,
  and queued-operation cancellation.

It exposes no generic shell, filesystem, Git, command execution, human gate
approval, extension authorization, release/deployment authorization,
destructive repair, or rebaseline tool. Server-side `CWApplication` policy is
authoritative.

**Technical capability does not imply governance authority.**

**Natural-language consent is not sufficient high-consequence authorization.**

OAuth scope possession, ChatGPT confirmation, repository access, or phrases
such as “yes, approve” cannot mint a high-consequence human grant.

## Branding, support, and security

The manifest display name is `CW — Codex Workflow`. It packages the canonical
square composer icon at `./assets/cw-mark-64.png`, the canonical full mark at
`./assets/cw-mark.png`, and the dark-background logo at
`./assets/cw-logo-dark.png`. These files are byte-identical to the canonical
repository assets.

- Non-sensitive support: <https://github.com/Queopius/cw/issues>
- Private vulnerability reports:
  <https://github.com/Queopius/cw/security/advisories/new>

Do not include credentials, OAuth tokens, Auth0 or Render secrets, tunnel keys,
device private keys, `.env` data, private source, raw local paths, or unrestricted
logs in support reports. No final Privacy Policy or Terms of Use is published by
this candidate; publication remains blocked on human/legal inputs.
