# Core 0.15.0 migration notes

CW Core/CLI `0.15.0` adds the local, governed `cw plan amend` command. It does
not change the project schema, governance evidence schema 2, or remote protocol
`cw.remote.v1`.

## Plan amendments

`cw plan amend` preserves the proposal-only workflow replacement mode and adds
an artifact-only mode for the current active, ungated phase. The active mode:

- accepts only explicit additions to `phase.artifacts`;
- requires workflow and state compare-and-swap hashes;
- supports mutation-free dry-run output;
- requires explicit human confirmation before apply;
- preserves the Completion Contract exactly;
- backs up the transaction and supersedes incompatible current-phase evidence
  append-only;
- returns the workflow to unapproved `PLAN_PROPOSED` and requires a separate
  `cw plan approve`.

It does not run the planner, reviewer, implementer, hooks, or agents. It is not
available through MCP or Remote. Do not edit `.cw` directly or repeat
`repair`/`start` to compensate for a declarative artifact omission. See
[Plan revisions](../plan-revisions.md) for syntax, recovery, and safety limits.

## Update and rollback

The Core updater accepts the Core-only `0.15.0` manifest with no Plugin
extension. A managed `0.14.1` installation can update to `0.15.0`, roll back to
`0.14.1`, and re-update without modifying consumer projects. Use the normal
[`cw update`](../updating.md) workflow; no project migration runs automatically.

## Independent Plugin lifecycle

Core `0.15.0` does not attach or republish a Plugin asset. The public
`cw-plugin-0.1.0.zip` from `v0.14.1` remains canonical at SHA-256
`b59275bb7e7a32e58c1d48202c9cf489874a6d21ce15fad3ef4cd6f202512021`.
Current Plugin source is an unpublished candidate. Publishing it requires a
separately authorized Plugin `0.2.0` release.
