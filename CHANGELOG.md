# Changelog

All notable changes to CW are documented here.

## Unreleased

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
