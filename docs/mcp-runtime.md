# MCP runtime: local read-only access

CW 0.8 provides an optional local MCP server for inspecting CW-controlled
projects from Codex and other local MCP clients. It uses stdio, invokes
`CWApplication` directly, and reads the same `.cw` evidence as the CLI.

```text
local MCP client
       │ stdio
       ▼
CW MCP adapter
       │ structured application calls
       ▼
CWApplication → CW engine → .cw evidence
```

This release is deliberately read-only. It is not a public ChatGPT plugin, a
hosted MCP endpoint, an Apps SDK UI, or a remote source-code service.

## Install the optional runtime

Normal CLI installation has no MCP dependency. From a source checkout, install
the extra in a virtual environment:

```bash
python -m pip install ".[mcp]"
```

The regular `cw` CLI and `cw.application` continue to import and run when that
extra is absent. Trying to serve MCP without the extra exits safely on stderr.

## Start a scoped stdio server

Authorize one initialized project at process startup:

```bash
cw mcp serve --project /absolute/path/to/project
```

The path is local bootstrap configuration supplied by the operator. MCP tool
calls do not accept project paths. They use the opaque repository handle
returned by the `cw://projects` resource. When exactly one project is
configured, tools may omit `project_id`.

Multiple projects and an explicit parent boundary are supported:

```bash
cw mcp serve \
  --allowed-root /absolute/path/to/workspaces \
  --project /absolute/path/to/workspaces/api \
  --project /absolute/path/to/workspaces/cli
```

CW resolves every path canonically, verifies Git and CW identity, and rejects
projects outside the allowed root, including symlink escapes. A client cannot
substitute `../../other-project` or a local path for an opaque handle.

## Connect local Codex

Current official OpenAI documentation supports command-started stdio servers
for local Codex clients. With the optional extra installed in the environment
that provides `cw`, configure the server:

```bash
codex mcp add cw -- cw mcp serve --project /absolute/path/to/project
codex mcp list
```

The same configuration can be expressed in Codex `config.toml` with a command
and arguments. See the official [Codex MCP documentation](https://developers.openai.com/codex/mcp/)
for the current configuration syntax and client support.

ChatGPT web does not read local Codex configuration. CW does not claim that
this local stdio process is a public or hosted ChatGPT integration.

## Tools

All tools accept an opaque `project_id` and optional safe `operation_id`. Every
tool is annotated read-only and is also enforced against CW's packaged
capability manifest.

| Tool | Result | Explicit limit |
| --- | --- | --- |
| `cw_project_status` | Canonical workflow, phase, gate, completion, and consistency facts | No mutation or arbitrary file read |
| `cw_project_inspect` | Project handle plus normalized evidence summary | No unrestricted source/log inspection |
| `cw_history` | Gate/review timeline and CW-owned history | No history mutation |
| `cw_explain` | Evidence-based reason the project can or cannot advance/complete | Does not repair |
| `cw_completion_status` | Contract, latest completion review, and extension proposal | Cannot review, authorize, or append |
| `cw_gate_status` | Normalized phase-gate states and consistency | Cannot create or approve gates |

There is no generic execute tool, shell, arbitrary filesystem read, arbitrary
Git operation, validator command, phase action, review action, repair action, or
extension-authorization action.

## Resources

The adapter exposes JSON resources containing normalized CW evidence:

- `cw://projects`;
- `cw://projects/{project_id}/summary`;
- `cw://projects/{project_id}/current-phase`;
- `cw://projects/{project_id}/gates`;
- `cw://projects/{project_id}/completion-contract`;
- `cw://projects/{project_id}/completion-review/latest`;
- `cw://projects/{project_id}/extension-proposal/current`.

Resources never provide arbitrary repository files, `.env`, process
environments, credentials, raw logs, or unrestricted reviewer transcripts.

## Read-only enforcement

The tool registry is a closed six-name allowlist. Before dispatch, the adapter
verifies that the tool's capability exists, is classified `READ`, and is not a
mutation. MCP calls always create a typed `mcp_client` operation context; input
cannot claim `human_cli`, reviewer, planner, or supervisor origin.

The adapter has no dynamic method name, command string, or hidden generic
capability dispatcher. Unsupported names return `AUTHORIZATION_REQUIRED` and
cannot reach application mutation methods. Tool annotations help clients, but
server-side capability enforcement is authoritative.

## Privacy and diagnostics

MCP results retain the opaque handle, display name, and repository identity but
remove `repository_root` and redact configured roots, home-directory prefixes,
credentials, token-like values, environments, and raw logs. Status does not
upload source content.

Runtime diagnostics include startup, tool name, opaque project handle,
structured error code, and shutdown only. They go to stderr; stdout is reserved
for MCP protocol frames. Python tracebacks and exception details are not normal
tool responses.

Repository files remain untrusted evidence. A README or `AGENTS.md` instruction
cannot change the allowlist, actor origin, capability class, gates, or
authorization policy.

## Lifecycle and failures

The runtime opens and validates configured projects before serving, handles
requests through the SDK's stdio lifecycle, and exits on EOF/client disconnect.
Malformed protocol input produces protocol diagnostics without mixing human
text into stdout. Application failures map to stable structured codes such as
`PROJECT_NOT_INITIALIZED`, `PROJECT_SCOPE_VIOLATION`, `STATE_INCONSISTENT`,
`OPERATION_CONFLICT`, and `INFRASTRUCTURE_FAILURE`.

Read requests create no CW evidence or transport-specific state. Retries are
harmless, and concurrent reads observe the same underlying state as the CLI.

## Why read-only first

CW is governance infrastructure. Read access proves project scoping, semantic
parity, protocol discipline, privacy, packaging isolation, and capability
enforcement before any conversational client can cause state changes.

The next milestone is **CW MCP Runtime · Controlled Actions**. It may consider
`validate`, `request review`, `start phase`, and `retry` only after trusted host
intent, operation idempotency, shared locking, and long-running lifecycle
contracts are proven. Extension authorization, rebaseline, and destructive
repair remain separate high-consequence operations requiring explicit human
authorization.
