# Configuration

Configuration precedence is:

```text
defaults < ~/.config/cw/config.toml < .cw/config.toml < command-line flags
```

`cw config` displays effective, non-secret settings. Workflow defaults are used
first, then global and project files override them. A newly initialized project
leaves its overrides commented out so it does not accidentally mask global
preferences.

CW v0.1 enforces `max_review_attempts`, `command_timeout`, and `review_timeout`
at runtime. Positive integers are required. `allow_network`, `protected_paths`,
and `human_gate_categories` are validated policy foundations for later policy
enforcement; they do not grant extra capabilities in v0.1. Unknown keys and
invalid TOML fail closed with a configuration error.

Plans also carry phase-specific required commands and reviewer timeouts. Commands
are never taken from the readiness manifest. A command-specific timeout takes
precedence over the effective default. JSON-formatted `phases.yaml` is intentional:
JSON is a valid YAML subset and keeps the core runtime dependency-free.

Global settings are preferences only. Project identity, plans, state, reviews,
gates, artifacts, and acceptance criteria are always repository-local.
