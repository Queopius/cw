# CW by Queopius

**Codex Workflow**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Plan, build, review, and advance software projects with Codex—one validated
phase at a time.

> **No valid gate. No next phase.**

CW is a standalone command-line product for explicit, reviewable AI-assisted
development. It separates planning from implementation, runs deterministic
checks before semantic review, invokes an independent read-only reviewer, and
records SHA-256 approval gates before allowing another phase to begin.

CW v0.1 is an early release. It is designed for local Git repositories and does
not claim unattended autonomy.

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

Installation copies all runtime files to `~/.local/share/cw/` and creates the
real launcher `~/.local/bin/cw`. The installed command does not depend on this
source checkout.

## How it feels

```text
CW by Queopius · Codex Workflow

  Project     shop-api
  Branch      main
  Workflow    ACTIVE
  State       IN_PROGRESS
  Plan        APPROVED

  Phase       02-authentication · Authentication
  Progress    2 / 4 phases
  Attempt     0 / 3

✓ 01  Repository Assessment
→ 02  Authentication
· 03  Billing
· 04  Release Verification

  Readiness   NOT READY
  Gate        PENDING
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
- Payment, cryptography, production, destructive migration, and similar goals can require a human gate.

## Commands

| Command | Purpose |
| --- | --- |
| `cw` / `cw start` | Start or resume the current phase |
| `cw init` | Initialize the current Git repository |
| `cw plan [show\|approve\|rebuild]` | Manage the plan lifecycle |
| `cw status` | Show concise workflow progress |
| `cw validate` | Run deterministic checks only |
| `cw review` | Run independent review after readiness |
| `cw retry` | Retry a retryable infrastructure failure |
| `cw history` | Show the phase audit trail |
| `cw doctor` | Diagnose environment and workflow integrity |
| `cw repair` | Back up and repair CW metadata only |
| `cw config` | Show effective non-secret configuration |
| `cw error` | Show the complete stored failure |
| `cw version` | Show the installed version |

Important read commands support `--json`; daily output respects `NO_COLOR` and
automatically removes ANSI escapes when stdout is not a TTY.

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
