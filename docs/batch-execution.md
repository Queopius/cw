# Controlled batch execution

CW supports bounded multi-phase execution without an unlimited autopilot mode.
The ordinary `cw` command remains a conservative single-phase operation.

```bash
cw run 3
cw run --phases 3 --max-time 2h
cw run --until 08-inventory
cw run 4 --dry-run
```

`cw run N` means “successfully approve and advance at most N consecutive
phases.” A semantic revision remains inside its phase and does not consume the
phase budget. Infrastructure failures consume elapsed time but not semantic
review attempts.

## Execution budget

The default policy is one phase, a recommended maximum of three, a hard cap of
ten, a two-hour batch budget, and three semantic revisions per phase. Durations
accept `30m`, `90m`, `2h`, and `1h30m`. The first reached limit or safety stop
wins.

Requests above the recommended limit are marked as extended. Six or more phases
require explicit confirmation; non-interactive runs use `--yes`. `--yes` never
bypasses the hard cap, human gates, invalid approval evidence, integration
requirements, or project identity checks.

`--until ID` and a phase count are deliberately mutually exclusive. The target
phase is included and must receive a valid gate before the batch succeeds.

## Gate and time safety

BatchRunner wraps the same supervisor used for a single phase. It never loops
over raw Codex calls. Dependencies are validated before each phase; advancement
must have been performed by the canonical approval transition; the resulting
gate is verified immediately; and every gate created in the session is checked
again before successful completion.

Elapsed budgets use monotonic time. CW stops starting new phases at the soft
deadline. An active implementer receives the remaining budget plus a bounded
five-minute grace period; its normal single-process timeout remains a separate
limit. Review calls retain their own bounded workflow timeout. There is no
unbounded reconnect or infrastructure retry loop.

## Stops and recovery

CW stops on human review, invalid gates, required integration failures, semantic
revision exhaustion, time exhaustion, workflow corruption, project mismatch,
infrastructure errors, user interruption, or workflow completion. Completed
gates, reviews, readiness, and the current workflow state are preserved.

Batch metadata lives at `.cw/runtime/batch.json`, separately from canonical
workflow state. A stale live session is presented as interrupted, never resumed
silently. Use `cw run --resume` only for an interrupted/stopped session with
remaining original phase and time budget. A fully exhausted budget requires a
new explicit batch.

`Ctrl+C` requests a safe stop. The child implementer is terminated through its
bounded process controller; no unfinished phase is marked approved. A new
`cw run N` after a stopped session starts a fresh budget.

## Estimates and history

Estimates use structured local phase-duration records. With insufficient
history CW says that estimation is unavailable; it never invents a completion
time or presents an estimate as a guarantee. Batch summaries are archived as
atomic per-session records under `.cw/logs/batches/` for local audit and future
estimates.

## Automation

`cw run N --dry-run --json` produces a stable preview without launching Codex.
Live batch JSON is intentionally not mixed with streamed agent output.
In CI/non-interactive use, large runs must be explicitly acknowledged and human
gates always stop. Exit code 4 represents safe partial completion caused by a
time budget; exit code 3 represents required human action.
