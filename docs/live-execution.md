# Live Codex execution

CW renders truthful checkpoints from the supported newline-delimited event
stream produced by `codex exec --json`. It does not infer normal activity from
Linux process tables, scrape reasoning text, or use a decorative spinner.

A normal long-running implementation progresses through observable facts:

```text
✓ Codex process started
✓ Session initialized

IMPLEMENTATION
────────────────────────────────────────────────────────────

→ Running command
  composer check

Elapsed      08m 41s
Activity     active
```

The exact command and timings come from real execution events. CW never invents
an activity description merely to animate the terminal.

## Lifecycle

The normalized lifecycle includes preflight, process startup, session
initialization, implementation, commands, file activity, validation, review,
gate creation, advancement, completion, stopping, and error states. The label
`Starting Codex session…` is valid only until the process and first structured
session event arrive.

Command events show a safe concise command and measured duration. File-change
events are aggregated to counts, with recent repository-relative paths limited
to verbose mode. Agent messages and reasoning events are not rendered or
persisted. Raw Codex stderr remains separate from the structured activity
stream; optional MCP diagnostics cannot override a valid exit-zero result.

Meaningful checkpoints include process/session startup, command start and
completion, aggregated file changes, implementation completion, validation,
review, gate creation, advancement, and final completion. CW remains a normal
line-oriented CLI rather than a full-screen terminal UI.

## Inactivity and timing

CW uses a monotonic clock for elapsed time and inactivity. After the configured
heartbeat interval with no event it may print a single `Codex still working`
checkpoint. After the quiet threshold it reports possible inactivity while
explicitly leaving the session running. An active command is not falsely
classified as stalled. Process exit, rather than silence, determines death.

```bash
cw config set observability.heartbeat_seconds 60
cw config set observability.quiet_threshold_seconds 90
cw doctor --performance
cw doctor --processes
```

Only timings CW measured are reported. Unavailable startup stages are omitted,
not estimated.

!!! note "Quiet is not dead"
    A live process with no recent events is reported as quiet or waiting. A live
    child command remains `RUNNING_COMMAND`. Only actual process exit is process
    death, and inactivity alone never terminates Codex.

## Runs and diagnostics

Every managed invocation has a `run_…` identity and a compact versioned event
log. PID is transient metadata, never durable identity.

```bash
cw inspect session
cw inspect run run_0123456789abcdef0123456789abcdef --verbose
cw logs --run run_0123456789abcdef0123456789abcdef
```

Run logs are redacted and retained in bounded form under `.cw/logs/runs/`.
An active run prevents another implementer from starting in the same project.
If the CW supervisor crashes, status reports an interrupted run and explicit
`cw repair` archives it before a safe restart; CW never launches a duplicate
agent blindly.

Use increasingly detailed views as needed:

```bash
cw status
cw inspect session
cw logs --run run_0123456789abcdef0123456789abcdef
cw doctor --performance
cw doctor --processes
```

- `status` shows whether the workflow or a managed run needs attention;
- `inspect` summarizes phase, session, activity, commands, files, and result;
- `logs` exposes bounded redacted structured events;
- `doctor --performance` shows measured startup stages;
- `doctor --processes` distinguishes current, stale, and unrelated processes.

## Output modes

- Normal mode prints bounded line-oriented checkpoints.
- `--quiet` retains events while suppressing non-critical progress.
- `--verbose` adds event types, paths, profiles, and sanitized diagnostics.
- `--json` emits JSONL records with no ANSI.
- CI/non-TTY output is deterministic and never uses a spinner or alternate
  screen.

The event stream does not change validation, review, gate, sandbox, human-gate,
or batch-budget semantics. No valid gate still means no next phase.
