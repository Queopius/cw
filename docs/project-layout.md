# Project layout

CW separates mutable workflow state from static Codex/project integration.

Typical structure:

```text
project/
├── .cw/
│   ├── project.json
│   ├── state.json
│   ├── gates/
│   ├── reviews/
│   ├── runtime/
│   ├── logs/
│   └── backups/
├── .codex/
└── AGENTS.md
```

## `.cw/`

Owned by CW for workflow operation, evidence, recovery, and history.

## `.codex/`

Static Codex-facing integration/configuration for the project.

## Protected paths

The implementer must not be allowed to rewrite workflow criteria or CW-owned evidence.

Exact protected paths are version/schema dependent and should be documented from source in each release.
