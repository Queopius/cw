# Plugin readiness architecture

CW 0.9 proves the multi-adapter architecture with an optional local governed
MCP runtime. It still does not ship a public plugin, an Apps SDK interface, a
hosted MCP endpoint, or a remote CW service.

```text
                         CW engine
                             │
                   application facade
                             │
             ┌───────────────┼───────────────┐
             │               │               │
          CLI adapter   governed MCP adapter   future skill
             │               │               │
        terminal / CI   ChatGPT / Codex   workflow guidance
```

All adapters must load the same `.cw` evidence and `.codex/workflow` contract.
There is no plugin-specific workflow state and conversation text is never
workflow evidence.

## Current OpenAI integration model

CW's boundary follows the current official OpenAI documentation, not the
retired 2023 plugin model:

- [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins)
  defines a plugin as skills, an optional MCP server, and optional UI resources.
- [Skills](https://developers.openai.com/plugins/concepts/skills) teach a
  repeatable workflow; the MCP server remains responsible for live data,
  authentication, authorization, and controlled actions.
- [MCP servers](https://developers.openai.com/plugins/concepts/mcp-server)
  expose narrow tools and resources with structured contracts.
- [Tool design](https://developers.openai.com/plugins/plan/tools) recommends
  separating operations with different permissions, risks, and confirmation
  requirements.
- [Security and privacy guidance](https://developers.openai.com/plugins/guides/security-privacy)
  requires server-side validation, least privilege, data minimization, and
  human confirmation for irreversible operations.

These product surfaces may evolve. CW engine code therefore imports no OpenAI
Apps SDK or MCP server package.

## Boundaries

### CW engine

`cw.core`, `cw.planning`, `cw.checks`, `cw.agents`, and `cw.execution` own:

- project and workflow validation;
- phase state and gates;
- Completion Contracts and completion evidence;
- independent reviews;
- extension proposals and authorization enforcement;
- repair, persistence, locking, and evidence integrity;
- sanitized subprocess execution selected by the workflow, never by an
  arbitrary adapter command.

The engine does not know about ANSI, terminal width, stdout, argparse, Apps SDK
components, MCP transports, or conversational text.

### Application layer

`cw.application` is the intentional internal API. `CWApplication` opens an
explicitly scoped project and returns `OperationResult` objects. Results carry
a schema version, operation ID, capability, opaque project ID, lifecycle state,
and structured data. Application errors have stable codes and do not require a
caller to parse a traceback or stderr.

The initial facade exposes status, explain, history, completion inspection,
repair, completion review, and extension authorization. Execution-heavy phase
orchestration remains supervised by the existing engine and is the next area to
move behind the facade before exposing write-capable MCP tools.

### CLI adapter

The CLI retains its parser, exit codes, concise rendering, color, update system,
and current commands. Context loading and status semantics now delegate to the
application layer. The CLI may include local-only presentation fields such as
the repository path; a remote adapter must apply its own minimum-disclosure
projection.

### MCP adapter

The adapter established in CW 0.8 and extended in CW 0.9 calls Python
application operations directly. It never runs
`subprocess("cw ...")`, accepts an arbitrary command, exposes a shell, or
maintains parallel state. Its stdio binding and optional SDK remain outside the
engine. See [MCP runtime](mcp-runtime.md).

### Future plugin skill

The skill explains how to use CW:

1. inspect structured state before acting;
2. work only on the current authorized phase;
3. treat `.cw` gates and completion evidence as authoritative;
4. never fabricate validation or review evidence;
5. distinguish planned-scope completion from target satisfaction;
6. show an extension proposal before asking for authorization;
7. never authorize a proposal on the model's own initiative.

The skill does not implement these rules. Engine policy remains authoritative.

## Capability model

The packaged `cw/application/capability-manifest.json` is transport-neutral.

| Capability | Class | Mutation | Human confirmation |
| --- | --- | --- | --- |
| `project.read`, `gate.read`, `history.read`, `completion.read` | READ | No | No |
| `validation.run`, `review.run` | EXECUTION | Controlled commands/evidence | No |
| `phase.start`, `project.repair` | STATE_MUTATION | Yes | Policy dependent |
| `extension.authorize` | HIGH_CONSEQUENCE_AUTHORIZATION | Yes | Always |

`phase.start` being declared does not make it remotely available. An adapter
publishes only the subset it can secure and supervise.

## Human authorization boundary

An extension approval needs a short-lived `AuthorizationGrant` tied to:

- the exact approve or reject action;
- the immutable current proposal reference;
- one operation ID;
- a typed actor origin;
- explicit user intent;
- issue and expiry timestamps;
- a one-use nonce.

Planner, reviewer, CI, and internal-supervisor origins are rejected even if
repository text tells them to approve. A consumed grant is persisted with the
authorization evidence. Repeating the same request returns an idempotent replay;
reusing its operation ID with different scope or evidence is an
`OPERATION_CONFLICT`.

The local CLI is itself the explicit human action boundary. A future ChatGPT or
Codex adapter must create the grant from trusted host confirmation metadata,
not from a model-supplied boolean or conversational claim.

CW 0.11 validated this separation in a real ChatGPT Pro read-only session: the
client correctly explained `HUMAN_REVIEW_REQUIRED` and refused to manufacture
the pending gate. A controlled state mutation remains categorically different
from high-consequence human authorization.

## Project scoping and identity

`ProjectResolver` accepts one or more trusted roots, resolves paths canonically,
then verifies the Git repository and existing CW identity. A path that escapes
an allowed root, including through a symlink, is rejected. Clients receive a
short repository identity handle and display name rather than relying on the
process working directory or exposing a full local path.

A resolver can keep several handles at once. This supports a future project
picker while preserving the CLI's convenient current-directory behavior.

## Local runtime and privacy

The local runtime owns repository access, `.cw`, Git, validators, and managed
Codex subprocesses. A conversational client receives approved CW operations,
not filesystem or shell authority.

Read results contain normalized workflow facts. Adapters must remove local path
fields and raw diagnostics before crossing a remote boundary. Source files,
`.env`, process environments, secrets, private logs, and complete reviewer
transcripts are not returned by a status call.

## Concurrency, retries, and operations

Every adapter uses the existing cross-platform `.cw/locks/operation.lock`.
Adapters do not create transport-specific locks. Mutations carry operation IDs;
high-consequence authorization is exactly-once for a matching ID and fails
safely on conflicting reuse.

`OperationResult` defines `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `BLOCKED`,
and `CANCELLED`. Current calls are synchronous. A later MCP runtime may place a
long-running operation behind start/poll endpoints without changing the domain
result. Cancellation means the operation stopped; it never means a phase
failed, creates a gate, or erases evidence.

## Prompt-injection boundary

Repository contents, AGENTS files, planner output, reviewer output, and tool
arguments are untrusted input. The authority order is:

```text
CW engine policy
  > phase and completion contracts
  > supervisor reviewer instructions
  > repository content
```

All structured model output is schema and semantically validated. No repository
file can mint an authorization grant, change actor origin, approve a gate, or
select an arbitrary command.

## Events and observability

`ApplicationEvent` defines a small transport-neutral vocabulary for project,
phase, validation, review, gate, completion, and extension transitions. This is
an integration view, not an event-sourcing rewrite. Existing gates, reviews,
state, and completion evidence remain authoritative.

## Packaging recommendation

The dependency-light optional `cw.adapters.mcp` package now lives beside the
engine so facade/adapter contract tests remain atomic. Keep future public plugin
packaging, OpenAI-specific metadata, and UI separately distributable. That
preserves independent release cadence and dependency footprint without splitting
repositories before a remote boundary is proven.

Open-source CW core and CLI remain fully local and functional without that
MCP extra, an account, internet access, or a hosted Queopius service.

## Implemented milestone: CW MCP Runtime · Controlled Actions

Validated outcomes:

1. candidate phase actions execute fully behind `CWApplication`;
2. typed MCP origin and application policy admit only the bounded action set;
3. start/poll/safe queued-cancel receipts cover long-running operations;
4. idempotency and shared locking apply across CLI and MCP;
5. only phase start, configured validation, independent review request, and
   narrow retry are exposed;
6. extension authorization, rebaseline, and destructive repair remain separate;
7. public ChatGPT HTTPS hosting, OAuth, Apps UI, packaging, and submission remain
   for later explicitly authorized milestones.
