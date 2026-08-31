# CW Versioning Policy

CW uses independent component versioning to avoid forcing product-wide releases for
small surface-level changes.

## Components

- **CW Core / CLI**: Python package, install/runtime behavior, CLI/API surface,
  and local MCP/remote adapters.
- **CW Plugin**: Candidate plugin metadata, branding, capabilities descriptors,
  and local manifest/skill package.
- **Remote protocol**: Message and transport contract between gateway and local
  agent.

## Current versions

- **CW Core / CLI**: `0.18.3`
- **CW Plugin**: `0.1.0` (unpublished functional-package candidate)
- **Remote protocol**: `cw.remote.v1`

The plugin version is intentionally independent. A Core `0.18.3` release can be
released without `0.1.x` changes when only core behavior changed. A plugin fix,
for example a metadata or skill clarification, can move to `0.1.1` without
requiring a core point release.

## SemVer policy

- **Core/CLI** and **Plugin** follow [Semantic Versioning](https://semver.org/).
- Core may be updated for:
  - bug fixes,
  - security fixes,
  - dependency and behavior corrections within the same major line.
- Plugin may be updated for:
  - capability descriptions,
  - skill wording,
  - packaging or manifest corrections,
  - compatibility metadata clarifications.
- Remote protocol is **compatibility-oriented** and should only be incremented when
  an incompatible protocol behavior change is introduced.

## Compatibility contract

`cw/adapters/mcp/plugin-compatibility.json` is the runtime authority for Plugin
compatibility. `plugins/cw/capabilities.json` mirrors the user-facing range and
the Plugin validator fails if it drifts from that packaged policy:

- `plugin_version` (for auditing and update checks)
- `cw_core.minimum`
- `cw_core.current_tested`
- `cw_core.compatible_policy`
- `remote_protocol.required`

Current policy for `0.1.0`:

- `cw_core.minimum: 0.14.0`
- `cw_core.current_tested: 0.18.3`
- `cw_core.compatible_policy: >=0.14.0,<1.0.0`
- `remote_protocol.required: cw.remote.v1`

Core and Plugin publication are separate ceremonies. Core `0.18.3` does not
change the Plugin version or its artifact name. The current Plugin source and
`cw-plugin-0.1.0.zip` build are unpublished candidates; this policy does not
authorize publication, tagging, or a future Plugin version.

## Future components

This policy is designed to support a future **CW Desktop** (or other adapter
components) with its own independent SemVer line without forcing unrelated
releases.
