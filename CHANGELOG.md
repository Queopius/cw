# Changelog

All notable changes to CW are documented here.

## Unreleased

## 0.3.4 — 2026-08-13

- Derive workflow progress from a fully validated contiguous approval-gate
  chain and fail closed before rendering or executing contradictory state.
- Reconcile stale current phase, last gate/review, attempt, readiness, errors,
  and missing approval history through backup-first `cw repair` without
  reopening valid phases or touching application code.
- Separate the semantic phase-contract fingerprint from mutable CW-managed
  project metadata while continuing to block implementation-agent metadata
  changes during a session.
- Clarify metadata version provenance with `created_with_cw_version` and use
  `cw_version` as the current document writer/migrator version.
- Add workflow-integrity status, doctor checks, `cw explain`, and regression
  coverage for impossible timelines, broken gate chains, and idempotent repair.

## 0.3.3 — 2026-08-13

- Remove all optional-MCP `mcp_servers.*` and plugin-disable overrides from
  managed Codex processes; use the user's normal effective configuration.
- Add a shared `CodexRunResult` for implementer, planner, and reviewer with
  separately captured stdout/stderr, structured payload, deduplicated optional
  integration diagnostics, and terminal-error precedence.
- Run the implementer through captured `codex exec`, retain redacted diagnostic
  logs, and add `cw doctor --codex --verbose` for sanitized argv inspection.
- Preserve explicit required-integration preflight while ensuring optional MCP
  authentication, HTTP, startup, or transport warnings cannot override a
  successful Codex result or consume semantic review attempts.

## 0.3.2 — 2026-08-13

- Fix managed Codex integration isolation for plugin-provided MCP servers by
  disabling optional plugins process-locally instead of reconstructing their
  effective definitions as `mcp_servers.*` overlays.
- Preserve user-owned Codex authentication/configuration, project Stop hooks,
  and required integrations while ensuring CW never emits an MCP `transport`
  property or modifies global Codex configuration.
- Classify rejected Codex MCP configuration as non-retryable
  `CODEX_CONFIG_ERROR`, preflight implementer configuration, and retain
  redacted argv/environment diagnostics without exposing prompts or secrets.
- Add managed build metadata and `cw version --verbose` paths/fingerprints so a
  stale installed runtime can be distinguished from the development source.

## 0.3.1 — 2026-08-13

- Trust GitHub's official `release-assets.githubusercontent.com` redirect host
  so verified public release manifests and artifacts remain downloadable while
  all non-GitHub update origins continue to fail closed.

## 0.3.0 — 2026-08-13

- Add controlled multi-phase execution through `cw run N`, `--phases`,
  `--until`, bounded duration budgets, safe previews, and interrupted-session
  resume without introducing an unlimited autopilot mode.
- Enforce default one-phase, two-hour, three-revision budgets; warn for extended
  runs; require explicit confirmation for large runs; and reject more than ten
  phases under the default global policy.
- Reuse the canonical single-phase implement/validate/review/gate supervisor and
  verify every new approval gate before advancement and again at batch completion.
- Persist batch state separately from workflow state, retain structured duration
  history, use monotonic runtime accounting, stop on human gates and errors, and
  preserve completed progress across interruption.
- Add safe global/project execution-policy precedence, dry-run JSON, batch status
  presentation, fake-clock regression tests, and an independent update lock guard.
- Isolate optional Codex MCP failures from planner/reviewer results, add required
  phase integration preflight, structured MCP diagnostics, deduplication, and
  read-oriented integration health commands without storing credentials.
- Recover a narrowly recognized orphan readiness after a retained REVISE result
  by backing up metadata, rerunning approved deterministic checks, binding a new
  session, and returning the same phase to review without reimplementation.

## 0.2.0 — 2026-08-13

- Add an explicit, consent-based CW update command with cached release checks,
  stable/beta/dev channels, release information, and machine-readable output.
- Introduce strict release manifests, trusted GitHub release origins, mandatory
  SHA-256 verification, hardened archive extraction, staged smoke tests, and an
  atomic managed-installation switch.
- Preserve the prior healthy installation for rollback, serialize updates with
  a global lock, recover interrupted staging, and retain a bounded version set.
- Migrate the canonical user installer to independent version directories and a
  stable launcher while protecting editable/source installations from updates.
- Keep CW application updates separate from project workflow migrations and
  make background update-check failures non-blocking and private by design.
- Fix planner structured-output compatibility and introduce the intentional
  `INITIALIZED` lifecycle/no-plan interface for clean repositories.

## 0.1.6 — 2026-08-13

- Persist clean repositories as `INITIALIZED` after `cw init` and render a
  dedicated no-plan screen without phase, validation, readiness, or gate UI.
- Prevent start and validation from running before a plan exists, with concise
  human-action guidance and no implementer invocation or workflow mutation.
- Separate full internal schemas from Codex-facing structured-output schemas,
  guard the latter centrally against unsupported keywords, and retain semantic
  uniqueness checks in the Python domain.
- Fix planning against Codex structured output by removing unsupported
  `uniqueItems` constraints from the Codex-facing plan schema while retaining
  them in CW's internal contract.
- Prioritize explicit API schema errors over unrelated MCP startup noise,
  classify schema incompatibility as non-retryable, and preserve successful
  structured results even when diagnostic stderr contains MCP failures.
- Run planner requests as direct external, ephemeral, read-only `codex exec`
  children using the user's normal authentication environment and separated
  stdout/stderr capture.

## 0.1.5 — 2026-08-13

- Replace the prototype-style flat status view with a width-aware workflow
  dashboard, a semantic progress bar, a dominant current-phase panel, a spaced
  plan timeline, concise health summaries, and contextual next actions.
- Establish a restrained reusable terminal visual system for headers, sections,
  alignment, wrapping, symbols, semantic color, and compact phase transitions.
- Redesign start, validation, review, doctor, history, diagnostics, help, and
  completed-workflow views around consistent daily, verbose, and machine modes.
- Preserve readable ASCII progress in colorless and redirected output, disable
  ANSI for `NO_COLOR` and non-TTY streams, and cap wide layouts while degrading
  cleanly in narrow terminals.
- Add colorless golden UI fixtures and visual regression coverage without adding
  a runtime presentation dependency.

## 0.1.4 — 2026-08-13

- Advance non-final approvals directly to the next configured phase with
  `IN_PROGRESS`, attempt zero, pending gate, and consumed readiness; complete
  the workflow immediately when the final configured phase is approved.
- Normalize legacy `PROPOSED` plans only when verified gates prove execution,
  and repair stale post-approval state without altering review or gate evidence.
- Reconstruct the phase audit view from validated gates and reviews, retaining
  revisions and recovered infrastructure failures while deduplicating legacy
  approval records around the canonical gate.
- Separate current position from verified approval count in status output and
  render invalid, approved, current, and pending phases from actual gate state.
- Add a dedicated terminal theme, symbols, and renderer layer with compact
  status, history, doctor, plan, start, review, and diagnostic presentation.
- Keep JSON free of presentation formatting, honor `NO_COLOR`, disable ANSI for
  non-TTY output, wrap long phase names, and reserve paths/details for verbose
  mode.

- Begin the v0.2 CLI modularization by extracting lifecycle, execution,
  validation, review, retry, status, history, doctor, diagnostics,
  configuration, and version use cases behind injected command services while
  preserving the public parser and compatibility seams.
- Separate argument normalization and the public grammar from injected command
  dispatch and its structured top-level error boundary.
- Add atomic, locked `cw config set` project overrides with strict typed value
  validation, safe-path enforcement, and consistent global flag placement.
- Recover prototype-era reviewer infrastructure failures through structured,
  retryable operation metadata without consuming semantic review attempts.
- Make `cw repair` back up and classify legacy reviewer system errors, preserve
  valid readiness, and correct historically inflated attempt counts.
- Make `cw retry` perform deterministic recovery: reuse valid readiness or
  validate existing artifacts and regenerate only the readiness manifest before
  invoking the independent reviewer.
- Centralize criterion severity as `blocking` and `advisory`, normalize the
  documented prototype alias `non-blocking` to `advisory` during backup-first
  repair, and keep unknown values fail-closed.
- Preserve and strictly validate prototype review/gate evidence without rewriting
  it, including hashes created before schema metadata was appended and archived
  inactive gates.
- Persist configured canonical severity in CW review records, reject reviewer
  severity reinterpretation, and surface failed advisory criteria as non-blocking
  observations.

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
