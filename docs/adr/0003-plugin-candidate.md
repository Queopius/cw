# ADR 0003: package CW as a local skill plus bundled MCP server

## Status

Accepted for candidate implementation.

## Context

CW 0.9 has a native-platform-verified local stdio MCP adapter. Current OpenAI
plugins package skills, MCP servers, or both under a required
`.codex-plugin/plugin.json`. ChatGPT web uses hosted plugin tools and does not
consume local Codex configuration.

## Decision

Package a repo-local `cw` plugin with one production skill and one bundled
stdio MCP server. Reuse the exact 0.9 MCP registry and `CWApplication`; add no
UI, hooks, `.app.json`, remote service, authentication, or capabilities. Keep
the candidate in the same repository so its version, parity tests, and security
policy evolve atomically with CW.

Classify Codex and ChatGPT desktop local hosting as candidate-supported.
Classify ChatGPT web and public submission as requiring a future authenticated
HTTPS MCP milestone.

## Consequences

Local users retain source and `.cw` state. The plugin requires CW with the MCP
extra and an initialized project. Public submission remains blocked by remote
runtime/authentication and human legal/business decisions. The plugin can move
to a separate release package later if remote dependencies or cadence diverge;
no repository split is justified yet.
