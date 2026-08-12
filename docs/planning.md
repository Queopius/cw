# Planning

`cw plan` inspects a bounded set of useful repository evidence: README and
roadmap files, architecture/TODO documents, selected Markdown under `docs/` and
`.github/`, common package manifests, source/test directory presence, and stack
markers.

Recognized stacks include PHP/Composer, Laravel, Node.js, Next.js, Python, Rust,
and Go. Stack detection provides planning hints and known deterministic test
commands; it does not assert an architecture.

When `docs/phases/` or a roadmap contains two or more explicitly numbered phase
headings, CW preserves that repository-defined shape. Otherwise it uses a small
goal-oriented fallback with baseline and release verification boundaries.

Use an explicit objective when documentation is ambiguous:

```bash
cw plan --goal "Implement Stripe subscriptions"
```

Plan states are distinct:

- `NOT_CREATED`: initialization completed but no work was inferred.
- `PROPOSED`: inspect with `cw plan show`; implementation cannot start.
- `APPROVED`: `cw plan approve` bound the plan hash to state and marked it READY.

`cw plan rebuild` creates a metadata backup before replacing an existing plan.
The internal `Planner` abstraction separates inspection, proposal, and validation
so a future AI planner or goal-scoped subworkflow can replace proposal logic.
