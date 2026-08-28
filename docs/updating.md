# Updating CW

CW follows Semantic Versioning for the application and versions project metadata
schemas independently. The default `stable` channel excludes prereleases;
`beta` and `dev` require explicit selection.

```bash
cw update --check
cw update --info
cw update
cw update --version X.Y.Z
cw update rollback
cw changelog
```

Automatic checks use a 24-hour cache and never install anything. They are
disabled in CI and can be disabled with `CW_NO_UPDATE_CHECK=1` or the global
`updates.check` preference.

```text
~/.local/share/cw/versions/<version>/
~/.local/share/cw/current -> versions/<version>/
~/.local/share/cw/update-state.json
~/.local/bin/cw
```

An update validates a strict manifest, selects the exact platform artifact,
downloads to private staging, verifies SHA-256, rejects traversal and links,
performs a staged smoke test, and atomically changes `current`. Failure leaves
the running installation selected. The previous healthy version remains for
rollback and retention is bounded.

Core 0.18.2 preserves project schema 1 and reads 0.18.1 verification receipts,
legacy readiness/reviewer infrastructure records, and pending human-authorized
legacy retries. Its Verification Executor isolates narrowly recognized
PHPUnit, PHPStan, Laravel, and Testbench caches, and its read-only Semantic
Reviewer accepts the current narrative-only Codex `agent_message` event while
remaining fail-closed for executable or unknown event types.

Core 0.18.1 preserves project schema 1 and reads legacy readiness/reviewer
infrastructure records. Existing receipt-free readiness is reverified once by
the Verification Executor before semantic review. Managed installation,
rollback, and re-update do not migrate Plugin 0.1.0, add tools, or change
`cw.remote.v1`/`cw.output.v1`. Historical `REVISE` recovery remains an explicit
operator action and is never performed by the updater.

Release manifest schema 1 may also contain an optional closed
`signature.extensions.plugin` section. Reusing the existing schema-v1 signature
object keeps Core 0.14.1 manifest consumers able to preserve and ignore the new
metadata during Core artifact selection. The section records the independently
versioned `cw-plugin-0.1.0.zip`, SHA-256, size, Core compatibility range, and
available source/build provenance. Core's
updater preserves this metadata but ignores it when selecting or installing a
Core platform archive. Manifests without the section remain valid.

Core `0.16.0` deliberately publishes a Core-only manifest without `signature`.
The 0.14.1 updater accepts this shape and treats omission as “no Plugin asset in
this Core release,” never as an uninstall or replacement instruction. Update,
rollback, and re-update affect only the managed Core runtime; Plugin sources,
project directories, and `.cw` evidence are untouched.

The production source is the Queopius CW GitHub release provider over trusted
HTTPS hosts. Tests use an injected local provider and real archives without
GitHub. The manifest reserves signing metadata, but CW currently guarantees checksum—not
signature—verification.

Source/editable installs refuse `cw update`; use the canonical installer while
developing. Updating CW never discovers or rewrites project data. Project schema
changes are handled only by backup-first `cw repair` inside that project.
