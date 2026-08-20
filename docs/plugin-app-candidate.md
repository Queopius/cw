# Plugin / App candidate

CW `0.12.0` retains the proven CW 0.11 plugin package and bounded ChatGPT
Developer Mode connection profile, then adds explicit production-readiness
contracts. It remains a local, reviewable
OpenAI plugin candidate. It is text/tool-first: one production skill,
one bundled stdio MCP server, official CW assets, and a repo-local development
marketplace entry. It is not published to the universal Plugins Directory and
does not provide a hosted service.

## Official model reviewed

This design was checked on 2026-08-15 against current official OpenAI docs:

- [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins)
- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [Submit plugins](https://developers.openai.com/plugins/deploy/submission)
- [Security and privacy](https://developers.openai.com/plugins/guides/security-privacy)
- [Apps SDK entry point](https://developers.openai.com/apps-sdk)
- [Codex MCP support](https://learn.chatgpt.com/docs/extend/mcp)
- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

The current model is not the obsolete 2023 `ai-plugin.json` convention. A
plugin has `.codex-plugin/plugin.json` and may bundle `skills/`, `.mcp.json`,
and optional UI. CW bundles a skill plus MCP and intentionally omits UI,
`.app.json`, hooks, remote authentication, and high-consequence actions.
The current Apps SDK entry point redirects into this unified Plugins model;
CW therefore does not add a legacy or parallel app manifest merely to claim an
app surface.

## Package shape

```text
plugins/cw/
├── .codex-plugin/plugin.json
├── .mcp.json
├── capabilities.json
├── assets/
└── skills/cw-workflow/
    ├── SKILL.md
    └── agents/openai.yaml
```

The plugin adapter starts the existing executable:

```text
cw mcp serve --allowed-root . --project .
```

It assumes the host launches the bundled stdio server in the active repository.
The repository must already be an initialized CW project and `cw` must be
installed with its MCP extra. The command does not initialize projects or
accept a caller-selected path through a tool.

## Install and enable locally

For an ordinary released CLI installation, install CW without plugin/MCP
dependencies:

```bash
python -m pip install codex-workflow
```

For this source-checkout candidate, install CW with its optional MCP support:

```bash
python -m pip install ".[mcp]"
```

From this repository, Codex and the ChatGPT desktop app can discover the repo
marketplace at `.agents/plugins/marketplace.json`. For an explicit non-default
marketplace installation:

```bash
codex plugin marketplace add /absolute/path/to/cw
codex plugin add cw@cw-development
```

These commands use the official development marketplace flow. They do not
publish the plugin or change the repository's CW state. A future published
package could use `codex-workflow[mcp]`; this candidate documentation does not
assume that version is already available from a package index.

Open an already initialized CW repository, enable the plugin, and inspect state
before acting. Repositories without `.cw` are rejected with the normal
structured project error. Initialization remains a CLI operation requiring
explicit user intent; it is not added to MCP by this candidate.

## Capability and permission mapping

| CW class | Plugin surface | OpenAI metadata | Enforcement |
| --- | --- | --- | --- |
| `READ` | status, inspect, history, explain, completion, gates, operation status | `readOnlyHint=true` | `CWApplication` capability policy |
| `EXECUTION` | configured validation, independent review | `readOnlyHint=false`, `destructiveHint=false` | trusted workflow/reviewer supervisor |
| `CONTROLLED_STATE_MUTATION` | authorized phase start, retry, queued cancel | `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true` | engine state machine and operation digest |
| `HIGH_CONSEQUENCE_AUTHORIZATION` | none | absent | rejected server-side |

The current plugin manifest has display-level capabilities, while individual
MCP annotations carry the supported read/write hints. Neither is trusted for
authorization. `plugins/cw/capabilities.json` records the precise CW mapping;
runtime contracts are compared against it in CI.

## Supported surfaces

| Surface | Candidate classification |
| --- | --- |
| Codex CLI / IDE local host | `READY` |
| ChatGPT desktop on the local Codex host | `READY` |
| ChatGPT web developer-mode app | `READ_ONLY_ACCEPTED`; real ChatGPT Pro + Secure MCP Tunnel evidence |
| Public Plugins Directory | `REQUIRES_REMOTE_APP_MILESTONE` |

### Codex local: candidate ready

Codex CLI, the Codex IDE extension, and the ChatGPT desktop app support local
stdio MCP on the same Codex host. The bundled server preserves local project
ownership and sends only normalized CW results.

### ChatGPT desktop local: candidate ready

The desktop Codex host can use local plugin marketplaces and stdio MCP where
account and workspace policy allow it. This is still local developer
distribution, not ChatGPT developer-mode app deployment or a public directory
listing.

### ChatGPT web development and public directory

ChatGPT web does not read local Codex configuration. CW 0.11 therefore adds
`cw mcp chatgpt-dev`, a startup-granted stdio profile intended for Secure MCP
Tunnel. The official tunnel can reach stdio directly, so CW adds no HTTP
listener. Real read-only UI acceptance passed with an authorized ChatGPT Pro
workspace and Secure MCP Tunnel on 2026-08-15. Controlled actions were not
enabled in that session. Public submission separately requires a production
remote architecture and public HTTPS. See
[ChatGPT development setup](chatgpt-development.md).

## Directory readiness classification

- Local installable/reviewable package: `READY`.
- ChatGPT web Developer Mode read-only connection: `ACCEPTED`.
- Public ChatGPT connection: `NEEDS_REMOTE_RUNTIME`.
- User-to-project connection lifecycle: `NEEDS_AUTH`.
- Final privacy/terms/support publication: `NEEDS_PUBLIC_DOCS`.
- Publisher identity, regions, attestations, contacts, and submission approval:
  `NEEDS_HUMAN_BUSINESS_INPUT`.
- Public directory submission: `BLOCKED` until those items are resolved.

## Future authenticated remote boundary

If ChatGPT web support is approved later, preserve this mapping:

```text
authenticated ChatGPT user
        ↓
scoped connection and opaque project grant
        ↓
remote-safe CW bridge
        ↓
typed ChatGPT/plugin actor
        ↓
CWApplication policy → local project
```

The next design must choose between a user-run reachable runtime, a secure
relay to a user-owned local runtime, or a managed service. It must not upload
source by default, equate ChatGPT identity with OS identity, or add arbitrary
shell/filesystem/Git access. OAuth, domain verification, retention, revocation,
and connection lifecycle require separate evidence.

## Deliberately unavailable

The plugin has no extension authorization, rebaseline, repair, state or gate
override, arbitrary workflow editing, contract replacement, deployment,
release, update, generic execute, shell, Git, or filesystem mutation. Optional
UI is deferred because normalized text and tool results cover the first
candidate without adding another security or maintenance surface.

The selected public topology, OAuth contract, and remaining submission blockers
are documented in [plugin production readiness](plugin-production-readiness.md).
No public gateway or OAuth implementation is included in 0.12.
