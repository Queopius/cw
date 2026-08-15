# Project layout

CW separates mutable workflow state from static Codex/project integration.
The MCP adapter uses these same locations and creates no parallel plugin state.

Typical structure:

```text
project/
├── .cw/
│   ├── project.json
│   ├── state.json
│   ├── gates/
│   ├── reviews/
│   ├── validation/
│   ├── completion/
│   │   ├── reviews/
│   │   ├── proposals/
│   │   ├── authorizations/
│   │   └── completion.satisfied.json
│   ├── runtime/
│   │   └── operations/
│   ├── logs/
│   └── backups/
├── .codex/
└── AGENTS.md
```

## `.cw/`

Owned by CW for workflow operation, evidence, recovery, and history.

Operational metadata such as current state, writer/schema version, history, and
migration records is mutable only through trusted CW transactions. Gates and
reviews are retained audit evidence. Runtime session/readiness files are scoped
to one managed execution and cannot be copied between projects.

Controlled adapter actions retain normalized validation evidence under
`.cw/validation/` and schema-versioned lifecycle/recovery receipts under
`.cw/runtime/operations/`. Operation identifiers remain inside records;
cross-platform hashed filenames prevent protocol IDs becoming local paths.
These receipts never replace workflow state, gates, reviews, or completion
evidence as sources of truth.

Contract-aware completion evidence is also mutable only through the CW
supervisor. Reviews, extension proposals, and human authorizations are
append-only. The singular completion gate is created only after a `SATISFIED`
system review and is authoritative for semantic completion.

## `.codex/`

Static Codex-facing integration/configuration for the project.

## Protected paths

The implementer must not rewrite workflow criteria or CW-owned evidence.

CW protects the **phase contract**: current phase definition, acceptance
criteria, dependencies, configured commands, relevant policy, integration
requirements, and human-gate requirements. Those are semantic inputs to the
review and cannot be changed by the agent whose work they judge.

CW also owns mutable operational metadata. A supervisor operation such as gate
creation, advancement, repair, or schema migration updates metadata and its
integrity baseline coherently; this is different from an implementation-agent
mutation.

At read/execute boundaries CW derives canonical state from configured ordering,
validated dependencies, the contiguous gate chain, cached state, readiness,
reviews, and history. Contradiction fails closed with `STATE_INCONSISTENT`; read
commands do not silently repair it.

Exact protected paths are version/schema dependent. Inspect effective policy
with `cw config` and integrity diagnostics with `cw doctor --verbose`.
