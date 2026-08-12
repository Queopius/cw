# Troubleshooting

Start with:

```bash
cw doctor
cw status
```

Normal status output shows a short classified error. Use `cw error` for stored
details and `cw error --raw` for scripts expecting the original diagnostic.

- **Project workflow mismatch:** run `cw repair`; CW backs up metadata first.
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
- **Another operation is active:** wait; if its process died, the next operation
  automatically recognizes the stale lock.
- **Plan goal unclear:** improve local documentation or pass `cw plan --goal`.

Detailed local diagnostics live under `.cw/logs/`. CW never prints Python stack
traces during normal daily commands.
