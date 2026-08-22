# Core 0.15.1 migration notes

CW Core/CLI `0.15.1` fixes read compatibility for projects initialized by Core
`0.14.1` that do not yet contain `.cw/supersessions`.

The missing directory means that no review supersession records exist. Read-only
operations therefore consume an empty index without creating metadata. This
includes `cw doctor`, `cw doctor --reviewer`, `cw audit`, `cw status`, `cw
history`, the Stop hook, and both human and JSON `cw plan amend --dry-run`
surfaces.

If the path exists, CW continues to reject files, symlinks, dangling symlinks,
special files, unexpected entry names, malformed JSON, invalid schemas, and
inconsistent record identities. Do not work around the compatibility issue by
creating the directory manually.

For an authorized active-workflow `cw plan amend --apply`, CW revalidates the
optional namespace after workflow/state CAS and under the exclusive operation
lock. If it remains absent, CW creates it only after the backup and transaction
journal are durable. A failed or interrupted operation restores its prior
absence; an existing directory and its records are preserved byte-for-byte.

The Plugin remains `0.1.0`, the remote protocol remains `cw.remote.v1`, and the
project and governance evidence schemas do not change. Managed installations
can update directly from Core `0.14.1` or `0.15.0` to `0.15.1` and retain the
normal rollback path.
