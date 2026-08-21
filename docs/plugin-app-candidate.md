# Plugin / App candidate

For the supported development/evaluation marketplace, immutable Git source,
safe ZIP staging, removal, and rollback flows, see
[Plugin packaging and installation](plugin-installation.md).

CW Core `0.14.1` and CW Plugin `0.1.0` retain the proven local plugin package
and bounded ChatGPT Developer Mode connection profile. The Plugin remains a
local, reviewable OpenAI plugin candidate. It is text/tool-first: one production
skill, one bundled stdio MCP server, official CW assets, a package README, and a
repo-local development marketplace entry. A staging HTTPS MCP gateway and OAuth
discovery exist for testing. They are not production services, are not wired
into this package through `.app.json`, and have not been submitted or published
to the universal Plugins Directory.

## Technical identity

- **Legal publisher:** Fantomid LLC
- **Technology brand:** Queopius
- **Product:** CW — Codex Workflow
- **Contact identity:** Queopius | Fantomid LLC
- **Website:** <https://cwcli.dev>
- **Documentation:** <https://docs.cwcli.dev>

Queopius is a technology brand operated by Fantomid LLC,
a New Mexico limited liability company.

## Official model reviewed

This current-state wording was rechecked on 2026-08-21 against official OpenAI
documentation:

- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Submit plugins](https://developers.openai.com/plugins/deploy/submission)
- [Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)
- [Authentication](https://developers.openai.com/plugins/build/auth)
- [Security and privacy](https://developers.openai.com/plugins/guides/security-privacy)

The current model is not the obsolete 2023 `ai-plugin.json` convention. A
plugin has `.codex-plugin/plugin.json` and may bundle `skills/`, `.mcp.json`,
and optional UI. CW bundles a skill plus local MCP and intentionally omits UI,
hooks, high-consequence actions, and a registered remote `.app.json`
connection. The separate staging runtime implements HTTPS MCP and OAuth
discovery for testing; it does not change the local package composition or
establish public availability.

## Package shape

```text
plugins/cw/
├── .codex-plugin/plugin.json
├── .mcp.json
├── README.md
├── capabilities.json
├── assets/
└── skills/cw-workflow/
    ├── SKILL.md
    └── agents/openai.yaml
```

The deterministic `cw-plugin-0.1.0.zip` uses `cw/` as its only root and also
contains byte-for-byte copies of the repository's canonical `LICENSE` and
`NOTICE`. The builder rejects missing, changed, symlinked, traversing,
case-colliding, or otherwise non-canonical members.

The plugin adapter starts the existing executable:

```text
cw mcp serve --allowed-root . --project .
```

It assumes the host launches the bundled stdio server in the active repository.
The repository must already be an initialized CW project and `cw` must be
installed with its MCP extra. The command does not initialize projects or
accept a caller-selected path through a tool.

Before registering any MCP tool, the installed runtime loads its packaged
Plugin compatibility policy and verifies Core `>=0.14.0,<1.0.0`. Missing,
malformed, older, or future-incompatible versions and missing or manipulated
policy data fail closed with `PLATFORM_CAPABILITY_UNAVAILABLE`; no partial tool
surface is registered. The Remote protocol remains `cw.remote.v1` with strict
negotiation.

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
| `CONTROLLED_STATE_MUTATION` | authorized phase start, retry, queued cancel | `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=false` | engine state machine and operation digest |
| `HIGH_CONSEQUENCE_AUTHORIZATION` | none | absent | rejected server-side |

The current plugin manifest has display-level capabilities, while individual
MCP annotations carry the supported read/write hints. Neither is trusted for
authorization. `plugins/cw/capabilities.json` records the precise CW mapping;
runtime contracts are compared against it in CI.

All 12 tools advertise closed input objects (`additionalProperties=false`) and
a closed, versioned result envelope. Runtime validation rejects the same
unknown parameters advertised by the schema. Reads are advertised idempotent.
Mutations are not advertised idempotent because `operation_id` remains optional
for compatibility; clients should supply and reuse a stable safe identifier.
The same identifier and payload replay the persisted operation, while reuse
with a different payload fails with `OPERATION_CONFLICT`.

## Supported surfaces

| Surface | Candidate classification |
| --- | --- |
| Codex CLI / IDE local host | `READY` |
| ChatGPT desktop on the local Codex host | `READY` |
| ChatGPT web developer-mode app | `READ_ONLY_ACCEPTED`; real ChatGPT Pro + Secure MCP Tunnel evidence |
| Staging HTTPS MCP/OAuth | `IMPLEMENTED_FOR_TESTING`; not a production or submitted service |
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
- Staging HTTPS MCP and OAuth discovery: `IMPLEMENTED_FOR_TESTING`.
- Production ChatGPT connection: `NEEDS_PRODUCTION_DEPLOYMENT`.
- Production user-to-project connection lifecycle: `NEEDS_PRODUCTION_AUTH`.
- Final privacy/terms/support publication: `NEEDS_PUBLIC_DOCS`.
- Publisher identity, regions, attestations, contacts, and submission approval:
  `NEEDS_HUMAN_BUSINESS_INPUT`.
- Public directory submission: `BLOCKED` until those items are resolved.

## Staging and future production boundary

The staging implementation tests this mapping; a future production deployment
must preserve it:

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

The selected model uses an HTTPS relay and a paired outbound-only local agent.
Staging exercises that implementation for testing. Production still requires
separate deployment, domain verification, retention/deletion decisions,
operational acceptance, and submission authorization. It must not upload source
by default, equate ChatGPT identity with OS identity, or add arbitrary
shell/filesystem/Git access.

## Deliberately unavailable

The plugin has no extension authorization, rebaseline, repair, state or gate
override, arbitrary workflow editing, contract replacement, deployment,
release, update, generic execute, shell, Git, or filesystem mutation. Optional
UI is deferred because normalized text and tool results cover the first
candidate without adding another security or maintenance surface.

The selected public topology, OAuth contract, and remaining submission blockers
are documented in [plugin production readiness](plugin-production-readiness.md).
No production public gateway, production OAuth deployment, marketplace
submission, or universal ChatGPT availability is claimed by Plugin 0.1.0.

The proposed next Plugin version is `0.2.0` because the published `0.1.0`
artifact is immutable and hardening tightened public contracts. That version is
**NOT AUTHORIZED** and no version file changes in this readiness wave.

## Remove or roll back locally

Remove the local development installation with `codex plugin remove cw` (or
the exact installed plugin identifier reported by `codex plugin list`). Remove
the repo-local marketplace separately only if it was added for this checkout.
Stopping/removing the Plugin does not edit project gates or `.cw` evidence.
Reinstall the unchanged published asset only through a separately authorized
rollback procedure; local hardening candidates are not published artifacts.
