# Configuration

Configuration precedence is:

```text
defaults < ~/.config/cw/config.toml < .cw/config.toml < command-line flags
```

`cw config` displays effective, non-secret settings. v0.1 supports foundations
for `max_review_attempts`, `allow_network`, `protected_paths`,
`human_gate_categories`, `command_timeout`, and `review_timeout`.

Plans also carry phase-specific required commands and reviewer timeouts. Commands
are never taken from the readiness manifest. JSON-formatted `phases.yaml` is
intentional: JSON is a valid YAML subset and keeps the core runtime dependency-free.

Global settings are preferences only. Project identity, plans, state, reviews,
gates, artifacts, and acceptance criteria are always repository-local.
