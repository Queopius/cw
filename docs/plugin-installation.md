# Plugin packaging and installation

**Development/evaluation distribution only.** These flows expose the CW Plugin
through a local or repository marketplace. They are not a workspace
publication and not a universal public publication. Plugin publication is not
authorized by this guide.

This page follows the current OpenAI documentation for
[Plugin packaging](https://developers.openai.com/plugins/build/plugins) and
[connecting and testing a Plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).
The commands below were verified with `codex-cli 0.150.1` on 2026-08-29.

## Prerequisites

- Codex CLI with the `codex plugin` command group, or a compatible ChatGPT
  desktop build for the manual Plugins Directory flow;
- Python 3.10 or later;
- CW Core installed with the MCP extra, within `>=0.14.0,<1.0.0`;
- CW Core `0.18.3` for the current tested combination; Plugin `0.1.0` remains
  compatible under its tested `>=0.14.0,<1.0.0` policy;
- a Git repository already initialized with `cw init` before MCP use;
- permission only for the repository root explicitly selected for the MCP
  process.

Installing the Plugin never initializes a project. A missing or incompatible
Core, missing MCP extra, missing `.cw`, invalid manifest, or root outside the
allowed set fails closed with an actionable error.

## Repository marketplace

The repository contains one marketplace at
`.agents/plugins/marketplace.json`. Its `./plugins/cw` source path is resolved
from the marketplace root, not from `.agents/plugins/` and not from the current
shell directory.

### Local checkout

From any clone of this repository:

```text
codex plugin marketplace add <CW_REPOSITORY_ROOT>
codex plugin marketplace list
codex plugin list --marketplace cw-development --available
codex plugin add cw@cw-development
codex plugin list
codex mcp list
```

Start a new conversation after installation so the host loads the installed
skill and MCP definition. Installation in the graphical Plugins Directory is
a separate `HUMAN_DESKTOP_ACCEPTANCE` step; the repository acceptance suite
does not simulate that UI.

### Immutable Git source

Use a full commit SHA or an immutable release tag. Never use `dev`, another
branch, or an unpinned default branch as a stable channel.

```text
codex plugin marketplace add Queopius/cw \
  --ref <IMMUTABLE_COMMIT_SHA> \
  --sparse .agents/plugins \
  --sparse plugins/cw
codex plugin marketplace list
codex plugin add cw@cw-development
```

Both sparse paths are required: the catalog alone cannot resolve the Plugin,
and the Plugin directory alone is not a marketplace.

## Candidate ZIP

`codex plugin` does not advertise direct ZIP installation. A candidate ZIP
must first be verified and safely expanded into a temporary local marketplace:

```text
python scripts/prepare_plugin_marketplace.py \
  --archive <CANDIDATE_ZIP> \
  --sha256 <EXPECTED_SHA256> \
  --destination <EMPTY_EVALUATION_DIRECTORY>
codex plugin marketplace add <EMPTY_EVALUATION_DIRECTORY>
codex plugin add cw@cw-development
```

The preparation command verifies the complete canonical inventory and digest,
then rejects corrupt archives, traversal, absolute or non-portable paths,
duplicates, case collisions, symlinks, special files, unexpected executables,
oversized expansion, and an existing destination. Extraction is staged and
renamed atomically, so failure leaves no partial marketplace and never
overwrites a previous evaluation directory.

## Refresh and pinned-source changes

Refresh a configured Git snapshot without changing its configured immutable
reference:

```text
codex plugin marketplace upgrade cw-development
```

An unchanged SHA must produce the same Plugin bytes. A source change at the
same Plugin version is not a safe public update after publication. This
candidate is not published; any future update or release still requires an
independent Plugin release decision.

The installed CLI does not expose an in-place `--ref` edit. Evaluate a new SHA
in an isolated Codex home first. Only after it passes, remove the installed CW
Plugin and its old marketplace source, add the newly pinned source, and install
CW again. Keep the prior SHA so the same sequence can restore it. Do not delete
projects, `.cw`, evidence, or unrelated Plugin configuration during this flow.

## Remove, source removal, disable, and rollback

These operations are distinct:

| Intent | Supported operation |
| --- | --- |
| Remove installed CW Plugin | `codex plugin remove cw@cw-development` |
| Remove marketplace source | `codex plugin marketplace remove cw-development` |
| Disable without removal | No `codex plugin disable` command in CLI `0.150.1` |
| Roll back | Re-add the prior immutable SHA and reinstall CW |

Remove the Plugin before removing its marketplace source:

```text
codex plugin remove cw@cw-development
codex plugin marketplace remove cw-development
```

Removal affects only the selected Plugin/source registration and cache. It
must not remove the project, `.cw`, governance evidence, Git state, other
plugins, credentials, sockets, or running user processes. Repeating Plugin
removal is idempotent in CLI `0.150.1`; repeating removal of an absent
marketplace source fails explicitly without changing other configuration.

## Current boundaries

- Core is `0.18.3`; its release does not republish or rename the Plugin.
- Plugin remains `0.1.0`.
- Remote protocol remains `cw.remote.v1`.
- The candidate artifact name is `cw-plugin-0.1.0.zip`.
- Local/repository marketplaces are development and evaluation channels.
- No production MCP/OAuth, workspace publication, universal publication, tag,
  release, or OpenAI submission is created by these procedures.
