# CLI reference

This reference summarizes CW's public command surface. It is intentionally
task-oriented rather than a raw `argparse` dump, and an offline repository check
keeps command names and options synchronized with the source parser.

Run `cw help` for the compact command index or `cw <command> --help` for the
installed build's exact grammar.

## Common output modes

Every named command accepts these presentation options:

| Option | Behavior |
| --- | --- |
| `--json` | Emit stable machine-readable output where the operation supports it. |
| `--verbose` | Include diagnostic detail and expanded evidence. |
| `--quiet` | Suppress normal human progress while retaining critical failures. |
| `--no-color` | Disable ANSI color; `NO_COLOR` and non-TTY output are also respected. |

Successful operations return `0`; workflow or operational failures normally
return `1`; invalid usage/configuration returns `2`; human action required
returns `3`; and an interrupted foreground operation returns `130`.

## Command index

| Command | Purpose |
| --- | --- |
| `cw` / `cw start` | Start or resume the canonical current phase. |
| `cw init` | Initialize CW in the current Git repository. |
| `cw plan` | Propose, inspect, approve, or rebuild a development plan. |
| `cw completion` | Inspect/review the Completion Contract or authorize an extension. |
| `cw status` | Show canonical progress derived from validated evidence. |
| `cw run` | Execute a bounded multi-phase batch. |
| `cw validate` | Run current-phase deterministic validation. |
| `cw review` | Run independent review or approve a pending human gate. |
| `cw retry` | Retry the classified retryable operation. |
| `cw history` / `cw explain` | Inspect audit history or a current blocker. |
| `cw inspect` / `cw logs` | Inspect managed executions and structured events. |
| `cw mcp` | Serve the optional local read-only MCP adapter over stdio. |
| `cw doctor` / `cw error` | Diagnose the environment or the latest failure. |
| `cw repair` | Reconcile CW metadata from validated evidence. |
| `cw config` / `cw integrations` | Inspect policy and integration state. |
| `cw update` / `cw changelog` | Manage verified CW releases and release history. |
| `cw version` / `cw help` | Inspect the build or command index. |

## cw

**Purpose:** Start or resume one current phase using the standard supervisor.
With no argument, `cw` is equivalent to `cw start`.

```bash
cw
```

CW refuses to launch an implementer when the workflow is inconsistent or
complete. It never means “run all remaining phases.”

## cw init

**Syntax:** `cw init`

Initializes project-local `.cw/` metadata and Codex integration files in the
current Git repository.

```bash
cw init
```

Fails with usage/configuration exit `2` outside a Git repository.

## cw start

**Syntax:** `cw start`

The explicit spelling of `cw`. It verifies project identity, workflow
consistency, dependency gates, protected inputs, and required integrations.

```bash
cw start --verbose
```

`--json` is rejected for implementation because a machine-output shortcut must
not silently launch a mutating agent.

## cw plan

**Syntax:** `cw plan [show|approve|rebuild] [--goal TEXT]`

- `--goal TEXT` supplies an explicit planning goal.
- `show` displays the proposed or approved plan.
- `approve` approves the current proposal without rewriting it.
- `rebuild` discards an unapproved proposal and plans again from bounded evidence.

```bash
cw plan --goal "Add subscription billing"
cw plan show
cw plan approve
```

Planner infrastructure failures preserve the pending goal for `cw retry`; an
invalid or partial plan is never installed.

## cw completion

**Syntax:** `cw completion [show|review|approve|reject|adopt] [--target TYPE]`

- `show` is the default and displays the contract, latest review, coverage, and pending proposal.
- `review` runs the independent read-only system completion reviewer.
- `approve` explicitly authorizes and appends the current extension proposal.
- `reject` records rejection without changing phases.
- `adopt` with `--target TYPE` explicitly adds a contract to a legacy workflow.

Supported adoption templates are `proof-of-concept`, `functional-prototype`,
`internal-tool`, `controlled-pilot`, `production`, and `public-release`.

```bash
cw completion show
cw completion review
cw completion approve
cw completion adopt --target controlled-pilot
```

`approve` is a supervisor-level human authorization boundary. A completion
reviewer or extension planner cannot invoke it or start proposed work.

## cw status

**Syntax:** `cw status`

Renders the effective workflow state, phase timeline, attempts, readiness, and
gate status. Counters derive from the validated contiguous gate chain.

```bash
cw status --json
```

This read operation explains inconsistent evidence and directs the user to
`cw repair` without silently mutating it.

## cw run

**Syntax:** `cw run [N] [--phases N] [--max-time DURATION] [--until PHASE] [--resume] [--dry-run] [--yes] [--non-interactive]`

- `--phases N` is equivalent to positional `N`.
- `--max-time DURATION` accepts bounded forms such as `30m`, `90m`, or `1h30m`.
- `--until PHASE` runs through one configured target and cannot accompany a phase count.
- `--resume` continues a safely recoverable batch with its original remaining budget.
- `--dry-run` previews scope without launching Codex.
- `--yes` answers confirmation but cannot bypass hard caps or human gates.
- `--non-interactive` disables prompts and fails if required confirmation is absent.

```bash
cw run 3 --max-time 2h
cw run --until 08-inventory --dry-run
```

The first phase/time/safety boundary wins. A completed workflow launches no phase.

## cw validate

**Syntax:** `cw validate`

Runs readiness, dependency, artifact, containment, configured command, and hash
checks for the current phase without semantic review.

```bash
cw validate --verbose
```

Returns human-action exit `3` when there is nothing ready to validate.

## cw review

**Syntax:** `cw review [--human-approve]`

Runs the independent read-only reviewer after validation. `--human-approve`
verifies and accepts a pending human gate; it is not an approval bypass.

```bash
cw review
cw review --human-approve
```

Only semantic `REVISE` consumes an attempt. Infrastructure failures are separate.

## cw retry

**Syntax:** `cw retry`

Retries the classified planning, implementation, or review operation. CW reuses
valid readiness for reviewer failures.

```bash
cw error
cw retry
```

It refuses deterministic configuration errors and completed workflows.

## cw history

**Syntax:** `cw history [--phase PHASE]`

Shows the audit timeline reconstructed from gates, reviews, and events.
`--phase PHASE` narrows the result.

```bash
cw history --phase 08-inventory
```

## cw explain

**Syntax:** `cw explain`

Explains why the workflow is blocked and names a safe recovery without writing.

```bash
cw explain
```

## cw mcp

**Syntax:** `cw mcp [serve] [--project PATH] [--allowed-root PATH]`

Starts the optional local read-only MCP runtime over stdio. `--project`
authorizes an initialized CW project and may be repeated. `--allowed-root`
constrains configured projects to a canonical local boundary and may also be
repeated. With neither option, the current project is the only allowed root.

```bash
cw mcp serve --project /absolute/path/to/project
```

Project paths are startup configuration supplied by the local operator; MCP
tool calls use opaque handles. Stdout is reserved for protocol messages and
diagnostics go to stderr. This command exposes no write, shell, filesystem, Git,
review, repair, or authorization operation. Install the optional
`codex-workflow[mcp]` dependency before serving.

## cw inspect

**Syntax:** `cw inspect [session|run|completion] [RUN_ID]`

`session` inspects the active/latest execution. `run RUN_ID` selects one record.
`completion` emits normalized Completion Contract, review, proposal, and cycle
evidence suitable for automation.

```bash
cw inspect session
cw inspect run run_0123456789abcdef0123456789abcdef --verbose
cw inspect completion --json
```

## cw logs

**Syntax:** `cw logs [--run RUN_ID]`

Reads the latest or selected redacted structured event log. `--run RUN_ID`
selects a stable CW run identity rather than a PID.

```bash
cw logs --run run_0123456789abcdef0123456789abcdef
```

## cw doctor

**Syntax:** `cw doctor [--reviewer] [--integrations] [--codex] [--performance] [--processes]`

- `--reviewer` adds a live ephemeral reviewer connectivity check.
- `--integrations` inspects configured integration health.
- `--codex` reports the latest sanitized managed Codex invocation.
- `--performance` shows measurable startup timings only.
- `--processes` distinguishes current, stale, and unrelated processes.

```bash
cw doctor
cw doctor --integrations --codex --verbose
```

The ordinary command is local; explicitly requested live checks may contact Codex.

## cw error

**Syntax:** `cw error [--raw]`

Shows the latest structured diagnostic. `--raw` includes complete redacted detail.

```bash
cw error
cw error --raw
```

## cw repair

**Syntax:** `cw repair [--reopen PHASE]`

Backs up and atomically reconciles CW metadata from validated evidence.
`--reopen PHASE` explicitly invalidates dependent gates after backup.

```bash
cw explain
cw repair
```

!!! warning
    Do not run `cw repair --reopen PHASE` as a generic retry. Normal repair does
    not fabricate approvals or modify application code.

## cw config

**Syntax:** `cw config [set KEY VALUE]`

Shows effective non-secret policy or validates and writes one supported setting.

```bash
cw config
cw config set execution.default_phases 1
```

## cw integrations

**Syntax:** `cw integrations [status|check|info] [NAME]`

`status` is the default summary, `check` performs health checks, and `info NAME`
shows one integration's requirement, health, and impact.

```bash
cw integrations status
cw integrations info vercel
```

## cw update

**Syntax:** `cw update [rollback] [--check] [--info] [--version VERSION] [--channel stable|beta|dev]`

- `--check` queries availability without installing.
- `--info` shows trusted release information.
- `--version VERSION` selects an explicit version.
- `--channel stable|beta|dev` changes channel for this invocation.
- `rollback` returns to the retained prior healthy version.

```bash
cw update --check
cw update --info
cw update rollback
```

Only one update action may be selected. Source installations are not self-updated.

## cw changelog

**Syntax:** `cw changelog`

Displays bundled trusted release history without contacting a release service.

```bash
cw changelog
```

## cw version

**Syntax:** `cw version`

Shows the installed version. Verbose mode adds installation paths, build/source
fingerprints, and stale-install detection.

```bash
cw version --verbose
```

## cw help

**Syntax:** `cw help`

Shows the compact public command index and examples.

```bash
cw help
```
