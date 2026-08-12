# Planning

`cw plan` inspects a bounded set of useful repository evidence: README and
roadmap files, architecture/TODO documents, selected Markdown under `docs/` and
`.github/`, common package manifests, source/test directory presence, and stack
markers.

The public command sends only that bounded evidence selection and detected stack
hints to an ephemeral Codex planning invocation. The planner runs read-only with
hooks, web search, ambient project instructions, and project execution rules
disabled. It returns only phases through a strict JSON Schema; CW itself supplies
project identity, policy, settings, and lifecycle state.

Recognized stacks include PHP/Composer, Laravel, Node.js, Next.js, Python, Rust,
and Go. Stack detection provides planning hints and known deterministic test
commands; it does not assert an architecture.

The internal deterministic planner preserves explicit phase headings when used
for offline tests and diagnostics. It is dependency-injected and does not make
network calls. Public `cw plan` uses the Codex backend so repositories are not
forced into one generic phase sequence.

Use an explicit objective when documentation is ambiguous:

```bash
cw plan --goal "Implement Stripe subscriptions"
```

Plan states are distinct:

- `NOT_CREATED`: initialization completed but no work was inferred.
- `PROPOSED`: inspect with `cw plan show`; implementation cannot start.
- `APPROVED`: `cw plan approve` bound the plan hash to state and marked it READY.

`cw plan rebuild` creates a metadata backup before replacing an existing plan.
The internal `Planner` abstraction separates inspection, proposal, validation,
and backend execution so future providers or goal-scoped subworkflows can reuse
the same fail-closed contract.
