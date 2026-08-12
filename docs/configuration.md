# Configuration

Configuration precedence is:

```text
defaults < ~/.config/cw/config.toml < .cw/config.toml < command-line flags
```

`cw config` displays effective, non-secret settings. Workflow defaults are used
first, then global and project files override them. A newly initialized project
leaves its overrides commented out so it does not accidentally mask global
preferences.

Project overrides can be written safely through the CLI:

```bash
cw config set allow_network true
cw config set max_review_attempts 5
cw config set human_gate_categories '["payments", "cryptography"]'
```

The setter accepts only known settings, validates the complete effective policy
before mutation, acquires the project operation lock, and atomically replaces
`.cw/config.toml`. Invalid values leave the file unchanged. List values use JSON
array syntax. Global preferences remain manually managed in
`~/.config/cw/config.toml`; `cw config set` intentionally changes only the
current repository.

CW v0.1 enforces `max_review_attempts`, `command_timeout`, `review_timeout`,
`allow_network`, and `human_gate_categories` at runtime. Positive integers are
required. Network access is denied by default for implementer shell commands;
when denied, live web search is disabled for that Codex invocation as well.
Human-gate categories determine which generated phases require explicit human
approval. `protected_paths` adds project files to the implementation-session
integrity snapshot. CW's state, identity, project configuration, gates, reviews,
and phase plan are mandatory protected paths and cannot be removed by an
override. Protected paths must be repository-relative, non-glob paths and cannot
be symlinks. Unknown keys and invalid TOML fail closed with a configuration
error.

`review_timeout` also bounds the structured, read-only planner call. Planner
transport failures and timeouts preserve the requested goal and can be retried
with `cw retry` without writing a partial plan.

Plans also carry phase-specific required commands and reviewer timeouts. Commands
are never taken from the readiness manifest. A command-specific timeout takes
precedence over the effective default. JSON-formatted `phases.yaml` is intentional:
JSON is a valid YAML subset and keeps the core runtime dependency-free.

Global settings are preferences only. Project identity, plans, state, reviews,
gates, artifacts, and acceptance criteria are always repository-local.
