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
  If the fingerprint proves this is the same Git repository under a new name,
  repair rebinds identity and preserves its plan. If the fingerprint belongs to
  another repository, repair quarantines that metadata in the backup and resets
  the active project to `NOT_CREATED` / `UNINITIALIZED`; run `cw plan` next.
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
- **Stale implementer session:** if no readiness exists, run `cw repair`; CW
  backs up metadata and removes the orphan lease. If readiness exists, run
  `cw review` so completed implementation is not restarted unnecessarily.
- **Implementer stopped without readiness:** inspect `cw error`, then run
  `cw retry`. CW removes the incomplete lease and starts a new implementer
  session without consuming a semantic review attempt.
- **Another operation is active:** wait; if its process died, the next operation
  automatically recognizes the stale lock.
- **Managed path cannot be a symlink:** replace the reported `.cw`, `.codex`, or
  managed file with a real repository-local directory/file. CW will not follow
  it or repair through it because doing so could modify data outside the repo.
- **Plan goal unclear:** improve local documentation or pass `cw plan --goal`.

Detailed local diagnostics live under `.cw/logs/`. CW never prints Python stack
traces during normal daily commands. Common tokens, authorization headers,
password assignments, and URL credentials are redacted before persistence, but
diagnostics should still be handled as sensitive local metadata.
