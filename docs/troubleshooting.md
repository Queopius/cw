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
  the active project to `NOT_CREATED` / `INITIALIZED`; run `cw plan` next.
- **Schema requires migration:** run `cw repair`. CW creates a metadata backup,
  upgrades known schema-less prototype records atomically, and leaves application
  source files untouched.
- **Criterion severity `non-blocking`:** this is a recognized prototype value.
  `cw repair` backs up the exact workflow and migrates it to the canonical
  `advisory` value. Current plans accept only `blocking` and `advisory`; every
  other value fails closed.
- **Metadata created by a newer CW schema:** upgrade CW before continuing. Do not
  use repair to downgrade it; CW deliberately leaves the newer document intact.
- **History integrity failure:** inspect `cw doctor --json` and the referenced
  file under `.cw/reviews/`, `.cw/gates/`, or `.cw/state.json`. CW will not delete
  or regenerate historical approval evidence automatically.
- **State/gate mismatch:** run `cw explain`, then `cw repair`. CW validates the
  contiguous gate chain, preserves every valid approval, archives the prior state
  in `.cw/backups/`, and advances cached state to the first phase without a gate.
  `cw status` deliberately does not repair or render a contradictory timeline.
- **Reviewer unavailable or timed out:** preserve readiness and run `cw retry`.
  CW records the failure as a retryable `review` operation and does not consume
  a semantic attempt. If an older project has only `reviewer_result: null` plus
  `system_error`, run `cw repair` first; repair backs up and classifies that
  record, corrects the attempt count, and preserves a valid readiness manifest.
  Direct `cw retry` also performs this backup-first migration when needed.
- **Legacy reviewer error with no readiness:** run `cw retry`. CW first checks
  current artifacts, dependency gates, and configured deterministic commands.
  If they pass, it regenerates only readiness and invokes the reviewer; it never
  reruns the complete implementation automatically. Missing or failing work
  remains in `ERROR` with an actionable validation failure.
- **Planner unavailable, invalid, or timed out:** CW preserves the pending goal,
  writes no partial plan, and `cw retry` reruns planning.
- **Implementer stopped unexpectedly:** CW preserves the current phase, records
  the process failure without consuming a semantic review attempt, and `cw retry`
  restarts the implementer rather than the reviewer. If the implementer already
  produced valid readiness, retry continues directly with review instead.
- **Codex configuration invalid:** inspect `cw error` and
  `cw doctor --codex --verbose`. CW classifies errors such as `invalid
  transport in mcp_servers.<id>` as deterministic `CODEX_CONFIG_ERROR`; blind
  retry is not offered. `cw version --verbose` shows the executable, runtime,
  build commit, and source comparison so an outdated managed install is visible.
  CW never writes a `transport` field, injects a partial `mcp_servers.*`
  override, or edits global Codex configuration. Redacted child stdout/stderr
  is retained in `.cw/logs/codex-runs.jsonl`; optional MCP warnings are
  diagnostic only when the Codex operation succeeds.
- **Approval gate invalidated:** do not overwrite the gate; run
  `cw repair --reopen <phase>`. CW backs up metadata and invalidates dependent
  gates before returning that phase to implementation.
- **Hook trust required:** review the hook in Codex with `/hooks`.
- **Readiness session mismatch:** the manifest belongs to an earlier implementer
  invocation. Inspect it, then restart the phase; do not copy runtime manifests
  between sessions or repositories. `cw repair` backs up and removes corrupt
  session/readiness pairs.
- **Orphan readiness after a retained revision:** when a valid `REVISE` review
  and protected-path stop prove the phase context, `cw repair` backs up the
  original metadata, reruns only commands from the approved plan, binds a fresh
  session, and returns that same phase to `READY_FOR_REVIEW`. It does not create
  a gate, reuse the prior review as approval, or modify application files.
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
- **Optional MCP returns HTTP 500:** run `cw integrations check`. CW reports the
  normalized provider error and impact without printing response HTML. Optional
  failures do not block unrelated phases.
- **MCP authentication required:** authenticate through Codex/provider tooling;
  CW does not own credentials. `invalid_token`/`AuthRequired` is classified
  separately from a provider HTTP 500.
- **Required MCP disabled or missing:** inspect `cw integrations` and the current
  phase's `required_integrations`. CW fails closed before implementation and
  never enables or edits global Codex configuration automatically.
- **Update check unavailable:** continue normal workflow use. Checks are cached
  and non-critical. Use `cw update --check` later for an explicit retry.
- **Update checksum or smoke test failed:** the active version was not switched.
  Inspect `cw error`; project data was not touched.

Detailed local diagnostics live under `.cw/logs/`. CW never prints Python stack
traces during normal daily commands. Common tokens, authorization headers,
password assignments, and URL credentials are redacted before persistence, but
diagnostics should still be handled as sensitive local metadata.
