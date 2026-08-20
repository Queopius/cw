# Changelog

All notable changes to CW are documented here.

## Unreleased

## 0.14.1 — 2026-08-20

- Add the standard project-independent `cw --version` surface while retaining
  `cw version` and the single Core version source of truth.
- Name future plugin release assets from the independent Plugin version and
  include the verified Core updater archive and manifest in release artifacts.

## 0.14.0 — 2026-08-16

- Prepare a portable, non-root Render staging image and Blueprint for the
  existing remote gateway, with exact deploy identity, readiness checks,
  bounded runtime configuration, managed TLS/custom-domain intent, and no
  provider SDK in CW.
- Introduce independent component versioning: Core/CLI remains
  `0.14.0` while the plugin candidate adopts `0.1.0`, with an explicit
  machine-readable plugin→Core→protocol compatibility contract and runtime
  readiness/build metadata reporting that keeps these versions separated.
- Retain the transactional SQLite backend on a single-instance encrypted
  staging disk, explicitly documenting deploy downtime and postponing a shared
  PostgreSQL backend until scale or availability evidence requires it.
- Add a real Auth0 staging contract for issuer/JWKS/resource validation, PKCE
  S256, preferred CIMD, guarded DCR fallback, narrow scopes, namespaced
  workspace identity, and independent device/project revocation.
- Add a local-agent staging profile, coherent environment contract, deployment
  and security runbooks, staging acceptance evidence, container/config
  validators, and sanitized build diagnostics without deploying or storing a
  credential.
- Preserve the exact remote tool surface, outbound-only agent, local source and
  `.cw`, provider-neutral Core/Application, and complete high-consequence
  authorization exclusion.

## 0.13.0 — 2026-08-15

- Add a hosting-neutral Streamable HTTP MCP gateway that exposes the accepted
  CW registry through `CWApplication`, with health/readiness endpoints,
  structured remote errors, limits, and no remote workflow engine.
- Add OAuth 2.1 protected-resource enforcement with issuer, signature,
  audience/resource, expiry, revocation, and least-privilege scope checks;
  validate PKCE S256 and CIMD/DCR authorization-server contracts without
  implementing an identity provider.
- Add an outbound-only, signed local CW agent using the versioned
  `cw.remote.v1` protocol, explicit single-use device pairing, opaque
  tenant-bound project grants, revocation, reconnect, and idempotent delivery.
- Prove all six reads and the six accepted controlled operations through the
  remote path while retaining local locks, independent review, gate policy,
  Completion Contracts, redaction, and the complete high-consequence denial.
- Add a transactional SQLite reference store for minimum routing/audit
  metadata, remote security/privacy/operations documentation, deterministic
  HTTP E2E tests, and native CI coverage via the optional `remote` extra.

## 0.12.0 — 2026-08-15

- Validate the CW plugin package against the current OpenAI Plugins model and
  preserve the exact accepted MCP surface, official assets, deterministic
  archive, and OpenAI-independent engine.
- Select a production architecture with a public HTTPS MCP gateway/relay and a
  paired outbound-only local CW agent so source and `.cw` remain local by
  default.
- Define OAuth 2.1 MCP authentication, least-privilege scopes, explicit project
  grants, revocation, replay protection, and a separate high-consequence human
  authorization ceremony.
- Add production threat, privacy/data-flow, observability, deployment,
  submission, and acceptance contracts without deploying a public service,
  adding OAuth code, or submitting the plugin.
- Keep ChatGPT Pro read-only by default and make controlled actions dependent
  on actual platform discovery; Business/Enterprise additionally requires
  workspace-admin opt-in and the same server-side CW policy.

## 0.11.0 — 2026-08-15

- Add a Secure MCP Tunnel-compatible ChatGPT development bootstrap over the
  existing stdio runtime, with no HTTP server, hosted service, or new CW tool.
- Add explicit startup project grants, fixed `chatgpt_app` origin, and separate
  read-only/controlled-action surface profiles with structured platform
  capability errors.
- Add deterministic ChatGPT-boundary tests for discovery, scope, privacy,
  prompt injection, actor forgery, replay/restart, and exact capability parity.
- Document current Developer Mode/tunnel permissions, setup, revocation,
  transmitted data, manual acceptance state, and the future authenticated
  relay contract without deploying or submitting anything.
- Close the external read-only acceptance gate with real ChatGPT Pro and
  Secure MCP Tunnel evidence, including structured project inspection,
  `HUMAN_REVIEW_REQUIRED` interpretation, project scoping, and refusal to turn
  conversational intent into human gate approval.

## 0.10.0 — 2026-08-15

- Package the accepted CW 0.9 engine and exact MCP capability surface as a
  current-format, repo-local OpenAI plugin candidate with no new workflow
  capabilities, hosted runtime, submission, or Apps SDK dependency.
- Add the production CW workflow skill, official brand assets, scoped stdio MCP
  definition, explicit capability mapping, and installable development
  marketplace metadata.
- Add deterministic plugin, skill, permission, security, privacy, semantic
  parity, archive, and official Codex CLI discovery/installation validation.
- Document the accurate surface split: local Codex and ChatGPT desktop can use
  the stdio candidate, while ChatGPT web/public distribution requires a future
  HTTPS remote runtime and authentication milestone.
- Add a milestone Completion Contract, listing draft, support/privacy/security
  drafts, transport/auth boundary, and CI package checks without submitting or
  publishing the candidate.

## 0.9.0 — 2026-08-15

- Add four narrow MCP controlled actions—authorized phase start, configured
  validation, independent review request, and engine-classified retry—through
  `CWApplication`, with no CLI subprocess or direct `.cw` editing.
- Add persistent project-scoped operation lifecycle, polling, idempotent replay,
  safe queued cancellation, stale-supervisor detection, structured progress,
  and cross-platform hashed operation records.
- Preserve review/gate/Completion Contract governance: clients cannot select a
  phase or validation command, supply review decisions, create gates, approve
  extensions, repair/rebaseline state, or invoke generic shell/filesystem/Git.
- Extend typed capability/origin enforcement, privacy redaction, expected
  mutation-set checks, cross-project isolation, concurrent CLI/MCP locking,
  fake-Codex controlled-flow acceptance, and MCP protocol/platform tests.
- Update local MCP, security, skill, contract, and transport documentation while
  retaining optional packaging and making no hosted/public plugin claim.

## 0.8.0 — 2026-08-15

- Add an optional local stdio MCP runtime whose six narrow read-only tools call
  `CWApplication` directly and share the CLI's workflow, gate, completion,
  history, and repair-derived semantics.
- Expose normalized project, phase, gate, Completion Contract, completion
  review, and extension-proposal resources using opaque project handles and a
  minimum-disclosure projection that removes private roots and secrets.
- Enforce a closed READ capability allowlist with typed `mcp_client` origin;
  arbitrary commands, shell, filesystem, Git, state mutation, review execution,
  repair, and extension authorization are absent and rejected.
- Add optional `codex-workflow[mcp]` packaging, `cw mcp serve`, clean stdio
  lifecycle/diagnostics, protocol/security tests, semantic-parity tests, and
  mutation-absence evidence across Linux, Windows, and macOS CI.
- Document local Codex configuration, privacy/scoping, the draft read-only CW
  skill, and the controlled-actions next milestone without claiming hosted or
  public ChatGPT support.

## 0.7.0 — 2026-08-15

- Add an OpenAI-independent application facade with structured operation
  results, stable errors, explicit project handles, and reusable status,
  explain, history, completion, repair, and completion-review operations.
- Make the CLI status/context path delegate to the same application semantics
  future adapters consume while preserving terminal UX and shared `.cw` state.
- Add a machine-readable capability model, cross-adapter operation lifecycle,
  canonical project scoping, shared locking, and idempotent operation identity.
- Enforce short-lived, proposal-bound explicit human authorization for workflow
  extensions; internal planner/reviewer origins and prompt-injected repository
  text cannot self-approve.
- Document the future narrow MCP tool/resource contract, plugin skill boundary,
  local runtime threat model, package isolation, and stdio-first transport ADR
  without shipping an MCP server or public plugin.

## 0.6.0 — 2026-08-15

- Add goal-derived Completion Contracts with extensible readiness templates and
  explicit requirement/evidence/severity semantics.
- Separate phase completion, planned-scope completion, and semantic product
  completion while preserving unchanged legacy completion behavior.
- Add independent read-only system completion review, strict normalized results,
  repository snapshot binding, and a distinct completion evidence gate.
- Add coherent extension proposals with explicit human approve/reject commands;
  proposed work cannot start automatically and previous phase evidence remains
  immutable.
- Extend canonical state derivation, repair, status, explain, inspect, history,
  fake-Codex acceptance, security guidance, and CLI drift protection across
  repeated completion cycles.

- Add native Windows user-local installation, centralized process/path/runtime
  activation portability boundaries, and UTF-8-safe subprocess and persistence
  behavior without changing workflow or gate schemas.
- Add installed-wheel deterministic acceptance with an external fake Codex,
  native Linux/Windows/macOS CI, manual real-Codex certification, recovery and
  update/rollback evidence, and an explicit evidence-based support policy.

## 0.5.1 — 2026-08-14

- Upgrade the public documentation experience with a task-oriented homepage,
  verified CLI and error references, responsive light/dark styling, and
  deterministic offline checks for command drift and internal links.
- Prepare the existing MkDocs Material documentation for strict Read the Docs
  builds at `https://docs.cwcli.dev`, with reproducible Python 3.13 build
  configuration and canonical product/source metadata.

## 0.5.0 — 2026-08-13

- Add a reproducible real-workflow recording pipeline for the future public CW
  landing-page hero, using a disposable Git repository and the installed CW
  product through normal planning, implementation, validation, independent
  review, gate, and completion behavior.
- Add a small stable public event schema, strict offline narrative/security
  validation, transactional last-known-good replacement, private-path and
  secret redaction, and deterministic fixture tests without exposing Codex
  reasoning or optional MCP diagnostics.
- Add explicit maintainer recording/dry-run commands and a network-free CI
  quality gate for the committed public artifact; ordinary tests never invoke
  Codex or require authentication.

## 0.4.2 — 2026-08-13

- Centralize gate-derived workflow truth in `EffectiveWorkflowState`, including
  canonical completion, current phase, approved/remaining/active counts, and
  final gate/review references.
- Add completed-workflow safety barriers to start, retry, CLI batch, and the
  domain `BatchRunner`; none may create an agent or batch after all gates pass.
- Classify all-approved state with an active phase as `STATE_INCONSISTENT` and
  retain strict fail-closed validation until explicit repair canonicalizes it.
- Add a pinned MkDocs Material documentation build to the main CI pipeline;
  `mkdocs build --strict` now fails release validation on documentation warnings.
- Establish the owner-supplied CW monogram as the canonical brand source, add
  deterministic dark/light/icon derivatives, and integrate it into README and
  MkDocs without adding assets or Pillow to the Python runtime package.

## 0.4.1 — 2026-08-13

- Fix completed-workflow repair so a fully validated gate chain converges to
  `COMPLETED` with no current phase instead of falling back to the first phase.
- Make completion an explicit reconciliation branch, preserve the final gate,
  clear stale readiness/errors, and keep repeated repair history-idempotent.
- Add completed repair/start UX: `cw repair` reports the complete gate count and
  `cw` never launches an implementer for an already completed workflow.

## 0.4.0 — 2026-08-13

- Stream documented `codex exec --json` JSONL events through one shared
  adapter so startup, session initialization, commands, file changes, token
  usage, completion, and failure remain observable without exposing reasoning.
- Replace the stale `Starting Codex…` wait with truthful line-oriented live
  states, elapsed time, command durations, debounced file summaries, restrained
  heartbeats, configurable inactivity warnings, quiet mode, and JSONL output.
- Persist redacted versioned run events and startup profiles under `.cw/logs`,
  add `cw inspect`, `cw logs --run`, `cw doctor --performance`, and portable
  managed-process diagnostics.
- Give runs durable IDs, protect projects from duplicate implementers, detect
  interrupted supervisors, and archive stale execution metadata through
  explicit repair without changing workflow gates or approval semantics.

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
