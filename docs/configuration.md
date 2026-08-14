# Configuration

## Precedence and effective policy

Configuration precedence is:

```text
defaults < ~/.config/cw/config.toml < .cw/config.toml < command-line flags
```

`cw config` displays effective, non-secret settings. Workflow defaults are used
first, then global and project files override them. A newly initialized project
leaves its overrides commented out so it does not accidentally mask global
preferences.

## Writing validated project settings

Project overrides can be written safely through the CLI:

```bash
cw config set allow_network true
cw config set max_review_attempts 5
cw config set human_gate_categories '["payments", "cryptography"]'
```

The setter accepts only known settings, validates the complete effective policy
before mutation, acquires the project operation lock, and atomically replaces
`.cw/config.toml`. Invalid values leave the file unchanged. List values use JSON
array syntax. Project policy settings change only the current repository.

## Update preferences

Update preferences are explicitly global:

```bash
cw config set updates.channel stable
cw config set updates.check false
cw config set updates.check_interval_hours 24
```

Equivalent `[updates]` keys live in `~/.config/cw/config.toml`. The supported
environment surface is `CW_NO_UPDATE_CHECK=1` and
`CW_UPDATE_CHANNEL=stable|beta|dev`; CI suppresses automatic checks by default.
Stable never selects prereleases.

## Live execution observability

Live execution uses conservative global observability thresholds:

```toml
[observability]
heartbeat_seconds = 60
quiet_threshold_seconds = 90
```

Set them with `cw config set observability.<key> N`. A heartbeat is emitted
only after real events have been quiet; the later warning is advisory and never
terminates Codex. Both use a monotonic clock. Quiet and JSON modes still retain
the redacted structured run record.

## Integration requirements

Integration requirements are project metadata, not connection configuration:

```toml
[integrations.vercel]
required = false
```

CW stores no MCP URLs, tokens, or provider credentials. A phase may additionally
declare `required_integrations`; only required capabilities participate in its
start preflight.

## Bounded execution budgets

Bounded execution preferences are global:

```toml
[execution]
default_phases = 1
recommended_max_phases = 3
hard_max_phases = 10
default_max_time = "2h"
max_semantic_revisions_per_phase = 3
```

They can also be set with `cw config set execution.<key> <value>`. Project
configuration may only reduce the global ceilings:

```toml
[execution]
max_phases = 4
max_time = "90m"
max_semantic_revisions_per_phase = 2
```

Command-line phase/time requests remain subject to the effective cap. This
prevents a repository from silently raising a user's global unattended-execution
limits.

## Workflow and sandbox policy

CW enforces `max_review_attempts`, `command_timeout`, `review_timeout`,
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
