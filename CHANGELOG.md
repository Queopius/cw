# Changelog

All notable changes to CW are documented here.

## Unreleased

- Add bounded, read-only Codex planning with strict structured output, local
  safety validation, and retryable infrastructure failures.
- Prevent generated plans from targeting CW metadata or supplying project
  identity, settings, and workflow state.
- Enforce the implementer network policy through the Codex workspace-write
  sandbox and disable web search when network access is denied.
- Apply configured human-gate categories during plan generation.
- Record unexpected implementer exits as retryable workflow infrastructure
  errors.

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
