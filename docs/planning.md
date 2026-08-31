# Planning

`cw plan` inspects a bounded set of useful repository evidence: README and
roadmap files, architecture/TODO documents, selected Markdown under `docs/` and
`.github/`, common package manifests, test configuration, source/test directory
presence, and stack markers. Codex also receives a depth- and count-limited
paths-only repository structure; source file bodies are not included merely
because they appear in that structure.

The public command sends only that bounded evidence selection and detected stack
hints to an ephemeral Codex planning invocation. The planner runs read-only with
hooks, web search, ambient project instructions, and project execution rules
disabled. It returns phases plus a Completion Contract through a strict JSON Schema; CW itself supplies
project identity, policy, settings, and lifecycle state.

The planner asks what evidence would prove the declared goal before deciding how
to group the work. Readiness classes are extensible templates, not phase-count
targets. Tests assert that POC, controlled-pilot, and production intent produce
appropriately different evidence contracts without asserting incidental wording
or a fixed number of phases.

Recognized stacks include PHP/Composer, Laravel, Node.js, Next.js, Python, Rust,
and Go. Stack detection provides planning hints and evidence-backed deterministic
commands; for example, `npm test` requires a real non-placeholder package script
and pytest requires repository configuration that declares pytest. Detection
does not assert an architecture.

The internal deterministic planner preserves explicit phase headings when used
for offline tests and diagnostics. It is dependency-injected and does not make
network calls. Public `cw plan` uses the Codex backend so repositories are not
forced into one generic phase sequence.

Use an explicit objective when documentation is ambiguous:

```bash
cw plan --goal "Implement Stripe subscriptions"
```

An initialized repository with no plan remains in the `INITIALIZED` runtime
state. `cw`, `cw validate`, readiness, and gate operations are unavailable until
a plan is proposed and approved. A README heading alone is not treated as a
development objective.

The planner runs as a direct external `codex exec` child of the global CW
supervisor. It is ephemeral, read-only, hook-disabled, and uses the user's normal
Codex authentication environment. CW captures stdout and stderr separately;
diagnostic MCP startup noise does not override exit code zero and a valid
structured result.

CW sends planner, reviewer, completion-reviewer, extension-planner, and
implementer prompts as exact UTF-8 bytes over the child process's standard
input. Prompt content is not placed in process arguments, environment variables,
or repository files. The transport accepts at most 4 MiB per prompt and creates
no prompt temporary files. This bound is well above the planner's bounded
repository-evidence budget while preventing unbounded process input.

Planning first persists the pending goal and `PLANNING` state, then starts the
read-only child. A classified launch, transport, process, or timeout failure
becomes a retryable `ERROR` with no plan or phase. If the host process stops
after that durable transition but before it can record the failure, `cw retry`
recognizes only the narrow `PLANNING` + `NOT_CREATED` state with a pending goal
and no plan hash, phase, review, or gate. It records recovery evidence and
retries the same goal. Any partially bound plan state fails closed instead.

Plan states are distinct:

- `NOT_CREATED`: initialization completed but no work was inferred.
- `PROPOSED`: inspect with `cw plan show`; implementation cannot start.
- `APPROVED`: `cw plan approve` bound the plan hash to state and marked it READY.

Runtime activity is represented by workflow state, not by mutating the static
plan on every phase. An executing workflow therefore has plan `APPROVED` and
runtime state `IN_PROGRESS`; a plan cannot remain `PROPOSED` while it executes.
During backup-first repair, CW may promote a legacy proposed plan only when
validated gates prove that configured phases already executed. Without reliable
evidence it remains proposed and fails closed.

`cw plan rebuild` creates a metadata backup before replacing an existing plan.
The internal `Planner` abstraction separates inspection, proposal, validation,
and backend execution so future providers or goal-scoped subworkflows can reuse
the same fail-closed contract.
