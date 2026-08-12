# Troubleshooting

Start with:

```bash
cw doctor
cw status
```

Normal status output shows a short classified error. Use `cw error` for the
structured stored details and `cw error --raw` for the complete redacted
diagnostic, including an internal traceback when available. This command reads
the independent diagnostic store and can still work if workflow state is corrupt.

- **Project workflow mismatch:** run `cw repair`; CW backs up metadata first.
- **Schema requires migration:** run `cw repair`. CW creates a metadata backup,
  upgrades known schema-less prototype records atomically, and leaves application
  source files untouched.
- **Metadata created by a newer CW schema:** upgrade CW before continuing. Do not
  use repair to downgrade it; CW deliberately leaves the newer document intact.
- **History integrity failure:** inspect `cw doctor --json` and the referenced
  file under `.cw/reviews/`, `.cw/gates/`, or `.cw/state.json`. CW will not delete
  or regenerate historical approval evidence automatically.
- **Reviewer unavailable or timed out:** preserve readiness and run `cw retry`.
- **Planner unavailable, invalid, or timed out:** CW preserves the pending goal,
  writes no partial plan, and `cw retry` reruns planning.
- **Implementer stopped unexpectedly:** CW preserves the current phase, records
  the process failure without consuming a semantic review attempt, and `cw retry`
  restarts the implementer rather than the reviewer.
- **Approval gate invalidated:** do not overwrite the gate; run
  `cw repair --reopen <phase>`. CW backs up metadata and invalidates dependent
  gates before returning that phase to implementation.
- **Hook trust required:** review the hook in Codex with `/hooks`.
- **Readiness session mismatch:** the manifest belongs to an earlier implementer
  invocation. Inspect it, then restart the phase; do not copy runtime manifests
  between sessions or repositories. `cw repair` backs up and removes corrupt
  session/readiness pairs.
- **Another operation is active:** wait; if its process died, the next operation
  automatically recognizes the stale lock.
- **Plan goal unclear:** improve local documentation or pass `cw plan --goal`.

Detailed local diagnostics live under `.cw/logs/`. CW never prints Python stack
traces during normal daily commands. Common tokens, authorization headers,
password assignments, and URL credentials are redacted before persistence, but
diagnostics should still be handled as sensitive local metadata.
