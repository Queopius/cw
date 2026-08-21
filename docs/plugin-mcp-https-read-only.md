# Plugin MCP HTTPS read-only profile

CW Plugin's remote testing profile is a **tool-only** MCP server using
Streamable HTTP. It has no UI and is not a public or production deployment.
The implementation follows OpenAI's current
[MCP server](https://developers.openai.com/plugins/build/mcp-server),
[tool design](https://developers.openai.com/plugins/plan/tools),
[authentication](https://developers.openai.com/plugins/build/auth), and
[security and privacy](https://developers.openai.com/plugins/guides/security-privacy)
guidance.

## Two deliberately different surfaces

| Surface | Transport | Tools | Intended environment |
| --- | --- | ---: | --- |
| Local Plugin | stdio | 12 | User-authorized local checkout |
| Remote read profile | Streamable HTTP at `/mcp` | 6 | Local/ephemeral tests and testing-only staging |

The HTTPS allowlist is exactly `cw_project_status`, `cw_project_inspect`,
`cw_history`, `cw_explain`, `cw_completion_status`, and `cw_gate_status`.
All advertise closed inputs, versioned structured outputs, `readOnlyHint: true`,
`destructiveHint: false`, `openWorldHint: false`, and `idempotentHint: true`.
The server rejects direct calls, aliases, case variants, and Unicode variants
of every other tool. In particular, operation status, phase start, validation,
review, retry, and cancellation remain local-only.

## Trust boundary

```text
authenticated MCP client -> HTTPS gateway -> opaque project grant
                         -> outbound-only agent -> authorized local CW root
```

The gateway cannot access a workstation filesystem. The local agent maps an
opaque handle to a root selected at startup and revalidates principal,
workspace, device, grant, and project. A tool cannot provide a path, URL, Git
reference, shell command, or routing decision. Repository text, README,
AGENTS.md, plans, logs, and tool results are untrusted data and cannot change
authorization or routing.

Every `/mcp` request requires the existing testing identity context and the
tool scope. Missing, invalid, expired, revoked, cross-user, cross-workspace, or
cross-project credentials fail closed. This is **not production OAuth**;
production OAuth and multi-tenancy acceptance remain a later wave.

## Transport and operations

- `/mcp` is the SDK Streamable HTTP endpoint and uses stateless requests.
- `/healthz` reports liveness only.
- `/readyz` reports a sanitized read-profile readiness result and fails with
  HTTP 503 if the required store schema cannot be read.
- Request size, per-principal/device rate, per-device work, HTTP concurrency,
  queue wait, agent idle time, and operation duration are bounded.
- Stateless mode retains no MCP session to expire; disconnect/cancellation
  releases the HTTP concurrency slot. Operation correlation remains tenant
  scoped in `cw.remote.v1`.
- Observability contains only event class, allowlisted tool name, outcome,
  latency, and active-request count. It omits payloads, results, tokens, paths,
  repository names, and project handles.

The configured gateway origin never comes from MCP inputs or project content.
Clients parse it as an origin, reject userinfo, ambiguous IP encodings,
non-global HTTPS IP literals, trailing-dot/Unicode confusion, paths, queries,
fragments, and invalid ports; redirects and environment proxies are disabled.
HTTP is allowed only for literal `127.0.0.1` or bracketed `::1` in local tests.
FastMCP host/origin validation remains enabled to resist inbound DNS rebinding.

## Local protocol acceptance

Run the remote test suite with the repository's remote extra. A protocol client
or MCP Inspector may connect to a locally started fixture at `/mcp`, provide a
testing token, initialize, list tools, and call a granted read tool. Never put a
real token in a command transcript or repository. ChatGPT Developer Mode
connection is `HUMAN_ACCEPTANCE`; automated tests do not claim that UI flow.

If the remote profile or agent is unavailable, remove the remote connection and
use the unchanged local stdio Plugin. No public Plugin, public endpoint, OAuth
production system, or OpenAI submission is created by this profile.
