# MCP runtime: local governed access

CW 0.9 extends the optional local stdio MCP runtime with four narrow controlled
actions. The six 0.8 inspection tools and all read-only resources remain. Every
request invokes `CWApplication` directly and uses the same engine, project lock,
state, gates, review pipeline, and Completion Contract semantics as the CLI.

```text
local MCP client
       │ stdio
       ▼
closed MCP tool registry
       │ typed OperationContext
       ▼
CWApplication → CW engine → .cw evidence
```

This is not a generally writable MCP server, public ChatGPT plugin, hosted MCP
endpoint, Apps SDK UI, or remote source-code service.

## Install and start

MCP remains optional:

```bash
python -m pip install ".[mcp]"
cw mcp serve --project /absolute/path/to/project
```

Ordinary CLI and application imports do not require the MCP SDK. The startup
path is local operator configuration only. Tools use opaque project handles,
never caller-provided filesystem paths. Multiple projects may be scoped beneath
explicit canonical roots:

```bash
cw mcp serve \
  --allowed-root /absolute/path/to/workspaces \
  --project /absolute/path/to/workspaces/api \
  --project /absolute/path/to/workspaces/cli
```

CW validates canonical containment, Git/CW identity, and symlink boundaries.
Operation IDs are scoped to the selected project and cannot be polled or
cancelled through another project handle.

## Connect local Codex

```bash
codex mcp add cw -- cw mcp serve --project /absolute/path/to/project
codex mcp list
```

See the official [Codex MCP documentation](https://developers.openai.com/codex/mcp/)
for current client configuration. ChatGPT web does not consume local Codex
stdio configuration; CW claims no hosted or public ChatGPT integration.

## Read tools and resources

| Tool | Capability | Mutation |
| --- | --- | --- |
| `cw_project_status` | `project.read` | None |
| `cw_project_inspect` | `project.read` | None |
| `cw_history` | `history.read` | None |
| `cw_explain` | `project.read` | None |
| `cw_completion_status` | `completion.read` | None |
| `cw_gate_status` | `gate.read` | None |
| `cw_operation_status` | `operation.read` | None |

The resources remain `cw://projects` and normalized per-project summary,
current-phase, gates, Completion Contract, latest completion review, and current
extension-proposal resources. Read tools/resources do not mutate workflow
evidence and never expose arbitrary repository files, `.env`, credentials,
process environments, or unrestricted logs.

## Controlled actions

All actions require an opaque `project_id` and caller-generated `operation_id`.
They return quickly with a durable lifecycle record; use
`cw_operation_status(target_operation_id=...)` to poll.

| Tool | Class | Required state | Intentional mutations | Explicit limit |
| --- | --- | --- | --- | --- |
| `cw_phase_start` | controlled state mutation | Current phase authorized/startable | Operation and implementation-session metadata | No phase argument; does not invoke Codex or create a gate |
| `cw_validate` | execution | Current phase active | Operation plus normalized validation evidence | No command argument; runs configured workflow checks only |
| `cw_request_review` | execution | Valid readiness for current phase | Operation, independent review, state, and possibly supervisor-created gate | Caller cannot provide decision, prompt, evidence, or sandbox |
| `cw_retry` | controlled state mutation | Current implementation/review infrastructure error retryable | Operation and narrow recovery/session/review evidence | No rewind, completed reopen, gate removal, planning/completion retry, or extension approval |
| `cw_operation_cancel` | controlled state mutation | Target still queued | Target operation lifecycle record | Running mutations are refused rather than rolled back |

Normal tool invocation is sufficient for these bounded actions because the
adapter assigns typed `mcp_client` origin and the application policy explicitly
admits only this set. Planner, reviewer, and internal-supervisor origins cannot
request them. Caller-supplied actor or authorization metadata is rejected.

## Operation lifecycle and idempotency

Operations use `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `BLOCKED`, and
`CANCELLED`. Results include a sanitized stage, phase/result references, timing,
and structured error. Validation failure and review rejection are semantic
results, not transport crashes; cancellation is not phase failure.

The first use of an operation ID binds it to project, action, capability,
origin, and canonical request digest. An identical replay returns the existing
record. Reuse for a different action/payload returns `OPERATION_CONFLICT` and
does not duplicate sessions, reviews, or gates. Records live under shared
`.cw/runtime/operations/`; their filenames are cross-platform hashes rather
than protocol IDs.

Safe queued cancellation is implemented. CW 0.9 intentionally refuses to kill
an already-running mutation because it cannot promise rollback of validation or
review evidence. Operations continue when a client disconnects while the local
server remains alive. After a server/process loss, operation polling marks a
stale supervisor record blocked and normal CW session/repair reconciliation
uses `.cw` as authority; there is no MCP-only database.

## Review and gate safety

An MCP client may request review but never acts as reviewer. CW invokes the same
independent sibling Codex process in read-only mode, validates its structured
result, and lets only supervisor logic create a gate after deterministic
validation and an approved semantic review. There are no `create_gate`,
`approve_gate`, or `force_gate` tools.

When the final authorized phase passes, the normal Completion Contract review
runs. `EXTENSION_REQUIRED` remains a human authorization boundary. MCP may read
the proposal but cannot approve it in CW 0.9.

## Security, privacy, and stdio

The closed registry has no arbitrary shell, Git, filesystem, validator command,
generic execute, repair, rebaseline, contract replacement, extension
authorization, release, deployment, or update tool. Capability annotations aid
clients; application policy is authoritative.

Repository text is untrusted evidence. README/AGENTS/source prompt injection
cannot select a phase, command, actor, review decision, gate, or authorization.
Responses pass through minimum-disclosure path/secret redaction. Detailed logs
stay local behind evidence references.

Stdout is reserved for JSON-RPC protocol frames. Safe diagnostics use stderr.
All validation, Git, and Codex subprocess paths explicitly isolate protocol
stdin so child processes cannot consume MCP frames.

## Intentionally unavailable

CW 0.9 does not expose extension authorization, rebaseline, destructive repair,
manual state/gate/review editing, arbitrary phase selection, release,
deployment, update installation, or any generic command interface.

The next milestone should evaluate **CW Plugin/App Candidate** before adding
high-consequence MCP administration. Such actions require a separate trusted
human-confirmation design and are not implied by controlled-action success.
