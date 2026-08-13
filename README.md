# CW by Queopius

**Codex Workflow**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Plan, build, review, and advance software projects with Codex—one validated
phase at a time.

> **No valid gate. No next phase.**

CW derives progress from the highest contiguous chain of validated gates. If
cached state, readiness, history, and gate evidence disagree, read commands
fail closed with an integrity explanation; `cw repair` performs backup-first
reconciliation, while `cw explain` describes the safe recovery without writing.

CW is a standalone command-line product for explicit, reviewable AI-assisted
development. It separates planning from implementation, runs deterministic
checks before semantic review, invokes an independent read-only reviewer, and
records SHA-256 approval gates before allowing another phase to begin.

CW v0.3 is an early release. It is designed for local Git repositories and
offers bounded—not unlimited—autonomy.

## Quick start

```bash
git clone git@github.com:Queopius/cw.git
cd cw
./install.sh

cd ~/code/my-project
cw init
cw plan --goal "Implement subscription billing"
cw plan show
cw plan approve
cw
```

Installation creates a versioned runtime under `~/.local/share/cw/versions/`,
atomically selects it through `current`, and creates the stable launcher
`~/.local/bin/cw`. The installed command does not depend on this source checkout.

## How it feels

```text
╭──────────────────────────────────────────────────────────────╮
│ CW · Codex Workflow                                   v0.3.2 │
│ by Queopius                                                  │
╰──────────────────────────────────────────────────────────────╯

  shop-api                                                  main
  ──────────────────────────────────────────────────────────────

  WORKFLOW                                              ● ACTIVE

  Progress    ########----------------------   25%
  Approved    1 / 4 phases
  State       IN PROGRESS
  Plan        APPROVED

  CURRENT PHASE
  ──────────────────────────────────────────────────────────────

  → 02 · Authentication

  Position      2 / 4
  Attempt       0 / 3
  Readiness     NOT READY
  Gate          PENDING

  DEVELOPMENT PLAN
  ──────────────────────────────────────────────────────────────

    ✓ 01  Repository Assessment

    → 02  Authentication

    · 03  Billing
    · 04  Release Verification

  ──────────────────────────────────────────────────────────────
  1 approved · 1 active · 2 remaining

  cw                Continue development
  cw validate       Validate current phase
  cw history        View audit trail
```

`cw doctor` groups environment, workflow, and security checks into a concise
health report. `cw history` presents the retained review and gate evidence as a
phase audit timeline rather than exposing raw event storage. All visual views
share the same symbols, section rhythm, bounded width, and contextual actions.

```text
╭──────────────────────────────────────────────────────────────╮
│ CW · Doctor                                                  │
╰──────────────────────────────────────────────────────────────╯

  Environment
  ✓ Git             /usr/bin/git
  ✓ Python          /usr/bin/python3
  ✓ Codex           /home/user/.local/bin/codex
  ✓ Repository      /home/user/code/shop-api

  Workflow
  ✓ Project identity
  ✓ Plan
  ✓ State
  ✓ Current phase
  ✓ Previous gates

  ──────────────────────────────────────────────────────────────

  ✓ Healthy
    10 checks passed · 0 warnings · 0 errors
```

```text
╭──────────────────────────────────────────────────────────────╮
│ CW · History                                                 │
╰──────────────────────────────────────────────────────────────╯

  ✓ 01 · Repository Assessment
      Approved · attempt 1

  → 02 · Authentication
      Current
```

## Workflow

```text
PLAN → IMPLEMENT → VALIDATE → INDEPENDENT REVIEW
                                  ├─ REVISE
                                  ├─ HUMAN REVIEW
                                  └─ APPROVE → GATE → NEXT PHASE
```

- Plans are proposed by an ephemeral read-only Codex invocation from bounded
  repository evidence, then validated locally and held for explicit approval.
- Implementers receive only the current phase and use `workspace-write`.
- Required commands run before AI review and come only from `phases.yaml`.
- Reviewers run independently with `read-only`, ephemeral sessions, and hooks disabled.
- Approved artifact hashes are rechecked before every dependent phase.
- A verified non-final gate advances state immediately to the next configured
  phase; the final gate completes the workflow.
- Payment, cryptography, production, destructive migration, and similar goals can require a human gate.

## Commands

| Command | Purpose |
| --- | --- |
| `cw` / `cw start` | Start or resume the current phase |
| `cw run N` | Run up to N phases within explicit safety budgets |
| `cw init` | Initialize the current Git repository |
| `cw plan [show\|approve\|rebuild]` | Manage the plan lifecycle |
| `cw status` | Show concise workflow progress |
| `cw validate` | Run deterministic checks only |
| `cw review` | Run independent review after readiness |
| `cw retry` | Retry a retryable infrastructure failure |
| `cw history` | Show the phase audit trail |
| `cw doctor` | Diagnose environment and workflow integrity |
| `cw repair` | Back up and repair CW metadata only |
| `cw config` | Show effective configuration or set a validated project override |
| `cw integrations [status\|check\|info]` | Inspect optional and required Codex integrations |
| `cw update [--check\|--info\|rollback]` | Check, inspect, install, or roll back CW releases |
| `cw changelog` | Show trusted bundled release history |
| `cw error` | Show the complete stored failure |
| `cw version` | Show the installed version |

Important read commands support `--json`; daily output respects `NO_COLOR` and
automatically removes ANSI escapes when stdout is not a TTY.

## Controlled multi-phase execution

CW can execute several phases consecutively, but autonomy is always bounded by
explicit phase, time, and review budgets:

```bash
cw run 3 --max-time 2h
cw run --until 08-inventory --dry-run
```

The first example authorizes at most three verified phase advancements and two
hours. Every phase still runs deterministic validation and independent review,
and a valid gate is required before the next phase starts. Human gates, invalid
gates, required-integration failures, exhausted semantic revisions, and time
limits stop the batch safely. `cw` remains the conservative single-phase
command. See [Controlled batch execution](docs/batch-execution.md).

## Updating CW

CW may check cached public release metadata, but it never installs an update
silently. Managed installations update explicitly:

```bash
cw update --check
cw update --info
cw update
cw update rollback
```

Every package is downloaded to staging, verified against its published SHA-256,
extracted with traversal protections, smoke-tested, and selected with an atomic
`current` pointer switch. The prior healthy version remains available for
rollback. Source/editable installations are protected from self-update. See
[Updating CW](docs/updating.md).

## Integration-aware workflows

CW separates capabilities required by the current phase from optional Codex
integrations. An unavailable deployment MCP does not block a domain phase that
does not use it. Managed children retain the normal effective Codex
configuration and capture optional MCP startup diagnostics without turning them
into workflow failures. Required integrations are preflighted explicitly. CW
never injects partial `mcp_servers.*` definitions, stores MCP credentials, or
silently changes `~/.codex/config.toml`. See [Integrations](docs/integrations.md).

## Project layout

```text
.codex/                 static Codex integration
  hooks.json
  hooks/phase_gate.py
  schemas/
  prompts/
  workflow/phases.yaml
.cw/                    mutable project state
  project.json
  state.json
  config.toml
  runtime/ reviews/ gates/ logs/ locks/ backups/
AGENTS.md
```

Project A and Project B never share these files. Identity checks fail closed if
workflow metadata does not match the current repository.

Read [Getting started](docs/getting-started.md), [Workflow](docs/workflow.md),
[Planning](docs/planning.md), [Gates and reviews](docs/gates-and-reviews.md),
[Configuration](docs/configuration.md), [Security](docs/security.md), and
[Architecture](docs/architecture.md) for details.
The promotion and tagging policy is documented in
[Release process](docs/release-process.md).

Maintainers can run the release-blocking, network-free two-repository isolation
demonstration with `make demo`.

## License

CW is licensed under the [Apache License 2.0](LICENSE).

Copyright 2026 Fantomid LLC.
