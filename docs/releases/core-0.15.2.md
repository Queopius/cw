# Core 0.15.2 migration notes

CW Core/CLI `0.15.2` fixes read compatibility for projects initialized by Core
`0.14.1` where both `.cw/supersessions` and `.cw/plan-revisions` may be absent.

The missing supersession directory means that no review supersession records
exist. The missing plan-revision directory is accepted only for coherent legacy
state: no active revision, no active revision hash, no superseded revisions,
and no history that requires persisted snapshots. If state or history declares
revisions, the snapshot files must exist and validate.

Read-only operations consume these namespaces without creating metadata. This
includes `cw doctor`, `cw doctor --reviewer`, `cw audit`, `cw status`, `cw
history`, the Stop hook, and both human and JSON `cw plan amend --dry-run`
surfaces.

Existing paths remain fail-closed. CW rejects files, symlinks, dangling
symlinks, special files, unexpected entry names, malformed JSON, invalid
schemas, inconsistent identities, and current-version projects where a required
namespace was removed. Do not work around compatibility by creating `.cw`
directories manually.

For an authorized active-workflow `cw plan amend --apply`, CW revalidates both
optional namespaces after workflow/state CAS and under the exclusive operation
lock. If either remains absent, CW creates it only after the backup and
transaction journal are durable. A failed or interrupted operation restores
prior absence; existing directories and records are preserved byte-for-byte.

The Plugin remains `0.1.0`, the remote protocol remains `cw.remote.v1`, and the
project and governance evidence schemas do not change. Managed installations
can update directly from Core `0.14.1`, `0.15.0`, or `0.15.1` to `0.15.2` and
retain the normal rollback path.
