# Changelog

All notable changes to CW are documented here.

## Unreleased

- Begin the v0.2 CLI modularization by extracting lifecycle, execution,
  validation, review, retry, status, history, doctor, diagnostics,
  configuration, and version use cases behind injected command services while
  preserving the public parser and compatibility seams.
- Separate argument normalization and the public grammar from injected command
  dispatch and its structured top-level error boundary.
- Add atomic, locked `cw config set` project overrides with strict typed value
  validation, safe-path enforcement, and consistent global flag placement.

## 0.1.3 — 2026-08-13

- Preserve review evidence as append-only records with exclusive atomic creation,
  including when an explicit phase reopen restarts semantic attempt numbering.
- Validate complete semantic review evidence before accepting human approval, and
  create approval gates atomically without replacing an existing gate.
- Capture artifact hashes after deterministic commands and revalidate dependency
  gates so command-side file changes cannot invalidate approved evidence silently.
- Require structured evaluation of every configured blocking criterion and
  repository-scoped file evidence from the independent reviewer.
- Unify the installed and internal reviewer schemas with strict summaries,
  fields, evidence types, and blocking-criterion results.
- Give planning bounded manifest content and a paths-only repository structure,
  while suggesting validation commands only when project configuration supports
  them.

## 0.1.2 — 2026-08-12

- Add bounded, read-only Codex planning with strict structured output, local
  safety validation, and retryable infrastructure failures.
- Prevent generated plans from targeting CW metadata or supplying project
  identity, settings, and workflow state.
- Enforce the implementer network policy through the Codex workspace-write
  sandbox and disable web search when network access is denied.
- Apply configured human-gate categories during plan generation.
- Record unexpected implementer exits as retryable workflow infrastructure
  errors.
- Enforce mandatory protected-path snapshots around implementer sessions and
  fail closed on workflow state, identity, configuration, review, gate, or plan
  tampering.
- Cross-validate approval gates against their independent review, complete
  criterion result, human-approval type, and exact artifact hash set.
- Bind readiness manifests to atomic implementer sessions, keep the Stop hook
  inert outside CW, consume successful/rejected readiness exactly once, and
  preserve infrastructure-failed readiness for reviewer-only retry.
- Centralize metadata schema compatibility, migrate schema-less prototype
  documents through backup-first repair, and reject future schemas without
  overwriting them.
- Audit every retained review, gate, state reference, and workflow history event
  through `cw doctor`, including historical phases.
- Persist atomic, redacted diagnostics independently from workflow state; make
  `cw error` usable during metadata corruption and keep unexpected exceptions
  compact while retaining their redacted traceback for raw diagnostics.
- Add owner-process leases for implementer sessions, reject parallel starts,
  repair orphan sessions safely, and treat a normal Codex exit without readiness
  as a retryable infrastructure failure.
- Reject `cw start --json` before state mutation instead of reporting a start
  that never launched an implementer.
- Validate the managed `.cw`/`.codex` filesystem topology before locks, init,
  context loading, repair, and backups; reject symlinks and special files that
  could redirect CW outside the current repository.
- Make repair fingerprint-aware: preserve workflows across legitimate repository
  renames, but back up and reset active project-specific metadata copied from a
  different Git repository, even when both directories share a basename.
- Add a reproducible offline release demo that runs against a copied installation,
  initializes two repositories, creates distinct plans, advances A through a
  verified gate, and proves B remains byte-for-byte unchanged.
- Check repository fingerprints before init-time legacy migration, preventing
  schema-less metadata from a same-named foreign repository from being modified
  or adopted.
- Preserve independently reviewed evidence across legitimate repository renames
  by atomically rebinding workflow IDs in reviews, gates, and session metadata,
  then validating the original hashes and history normally.

## 0.1.1 — 2026-08-12

- Execute deterministic validation commands without a shell and reject shell
  control syntax in workflow configuration.
- Enforce global and project policy precedence for review attempts and command
  and reviewer timeouts.
- Validate policy types, unknown keys, and malformed TOML with concise errors.

## 0.1.0 — 2026-08-12

- Licensed CW under Apache-2.0, copyright 2026 Fantomid LLC.
- Established the public product brand as CW by Queopius.
- Introduced the standalone `cw` Python package and global installer.
- Added repository-scoped initialization, planning, state, review, and gates.
- Added deterministic validation, independent read-only review, and human gates.
- Added atomic persistence, operation locks, repair backups, JSON output, and tests.
