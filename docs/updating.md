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

The production source is the Queopius CW GitHub release provider over trusted
HTTPS hosts. Tests use an injected local provider and real archives without
GitHub. The manifest reserves signing metadata, but CW currently guarantees checksum—not
signature—verification.

Source/editable installs refuse `cw update`; use the canonical installer while
developing. Updating CW never discovers or rewrites project data. Project schema
changes are handled only by backup-first `cw repair` inside that project.
