# Integration-aware workflows

CW distinguishes core requirements (Git, Python, Codex, project metadata and
required CW hooks) from optional Codex integrations such as deployment, design,
or browser MCP servers. An optional server failure has no workflow impact unless
the current phase explicitly requires it.

```bash
cw integrations
cw integrations check
cw integrations info vercel
cw doctor --integrations
```

Health is `AVAILABLE`, `UNAVAILABLE`, `AUTH_REQUIRED`, `DISABLED`, or `UNKNOWN`;
requirement is `REQUIRED`, `OPTIONAL`, or `UNUSED`. Repeated startup diagnostics
are deduplicated and large HTTP bodies stay out of normal output. `--verbose`
exposes bounded process diagnostics for deliberate troubleshooting.

```toml
[integrations.vercel]
required = true
```

A phase may declare `required_integrations: ["vercel"]`. CW actively preflights
only required integrations and fails closed if one is missing, disabled,
unauthenticated, or unavailable. Optional servers are not health-checked during
`status`, `history`, or `help`.

Planner and reviewer calls preserve the user's authentication while disabling
optional plugins and hooks for that read-only child process. Implementers use
Codex's effective `mcp list` as the source of truth rather than reconstructing
servers from `~/.codex/config.toml`: optional plugin integrations are isolated
with the supported process-scoped `--disable plugins` feature, while standalone
optional MCPs may receive only `mcp_servers.<id>.enabled=false`. CW never creates
or copies `url`, `command`, `type`, or unsupported `transport` fields.

If a phase requires an integration, CW leaves it enabled and preflights it
before starting implementation. Project Stop hooks remain available to the
implementer; disabling optional plugins does not disable those project hooks.
CW does not edit global Codex configuration, auto-trust hooks, or store OAuth
tokens, API keys, cookies, or MCP secrets. Authentication remains owned by Codex
and the provider.
