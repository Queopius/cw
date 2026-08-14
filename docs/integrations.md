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

## Optional and required integrations

```toml
[integrations.vercel]
required = true
```

A phase may declare `required_integrations: ["vercel"]`. CW actively preflights
only required integrations and fails closed if one is missing, disabled,
unauthenticated, or unavailable. Optional servers are not health-checked during
`status`, `history`, or `help`.

| Requirement | Preflight before agent launch | Failure impact |
| --- | --- | --- |
| `REQUIRED` | Yes | Phase stops safely |
| `OPTIONAL` | No | Diagnostic warning only if Codex otherwise succeeds |
| `UNUSED` | No | No workflow impact |

## Effective Codex configuration

Planner, reviewer, and implementer calls preserve the user's normal effective
Codex configuration. CW does not add `mcp_servers.<id>.enabled=false` or any
other partial MCP override: effective definitions may originate from plugins,
profiles, or other Codex-owned sources that CW must not reconstruct. Stdout and
stderr are captured separately. Optional startup, authentication, HTTP, and
transport diagnostics are deduplicated and retained as non-blocking warnings
when Codex exits successfully with the expected result.

!!! note
    CW treats Codex's effective configuration as authoritative. It does not
    reconstruct plugin/profile-managed MCP definitions from one TOML file.

## Authentication and diagnostics

If a phase requires an integration, CW leaves it enabled and preflights it
before starting implementation. Project Stop hooks remain available to the
implementer.
CW does not edit global Codex configuration, auto-trust hooks, or store OAuth
tokens, API keys, cookies, or MCP secrets. Authentication remains owned by Codex
and the provider.

Use `cw doctor --codex --verbose` to inspect the latest sanitized managed argv.
It reports whether an unsupported `mcp_servers.*` override was present without
exposing the prompt or credentials. Redacted raw child diagnostics are retained
in `.cw/logs/codex-runs.jsonl` for deliberate troubleshooting.
