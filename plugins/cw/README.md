# CW — Codex Workflow Plugin

CW Plugin combines the `cw-workflow` skill with the scoped local CW MCP stdio
server. It operates only on an already initialized project explicitly granted
to the runtime and exposes no generic shell, filesystem, Git, release, deploy,
repair, or gate-approval tool.

## Technical identity

- **Legal publisher:** Fantomid LLC
- **Technology brand:** Queopius
- **Product:** CW — Codex Workflow
- **Contact identity:** Queopius | Fantomid LLC
- **Website:** <https://cwcli.dev>
- **Documentation:** <https://docs.cwcli.dev>
- **License:** Apache-2.0; see the bundled `LICENSE` and `NOTICE`

Queopius is a technology brand operated by Fantomid LLC,
a New Mexico limited liability company.

## Distribution status

| Surface | Status |
| --- | --- |
| Local MCP stdio | `IMPLEMENTED` |
| Staging MCP HTTPS | `IMPLEMENTED_FOR_TESTING` |
| Staging OAuth/discovery | `IMPLEMENTED_FOR_TESTING` |
| Production MCP HTTPS | `NOT_DEPLOYED` |
| Production OAuth | `NOT_DEPLOYED` |
| OpenAI domain verification | `NOT_COMPLETED` |
| Universal submission | `NOT_CREATED` |
| Public Plugin publication | `NOT_COMPLETED` |

The staging service is test infrastructure, not a production service or a
publicly submitted Plugin. The package intentionally has no production
`.app.json` connection.

## Support and security

- Technical documentation: <https://docs.cwcli.dev>
- Non-sensitive support: <https://github.com/Queopius/cw/issues>
- Private vulnerability reports:
  <https://github.com/Queopius/cw/security/advisories/new>

Do not include credentials, private source, `.env` data, raw paths, reviewer
prompts, or unrestricted logs in support reports.

## Legal and version boundaries

No final Privacy Policy or Terms of Use is published by this candidate. Any
legal draft requires human and legal review and is not a submission document.

- Core: `0.14.1`
- Plugin: `0.1.0`
- Remote protocol: `cw.remote.v1`
- Proposed next Plugin version: `0.2.0` — **NOT AUTHORIZED**

The published Plugin `0.1.0` archive remains immutable. Any archive built from
this source tree is an unpublished local candidate.

## Development and evaluation installation

This repository marketplace is a development/evaluation source only. It is not
a workspace publication and not a universal public publication.

From a local checkout, use the commands supported by the installed Codex CLI:

```text
codex plugin marketplace add <CW_REPOSITORY_ROOT>
codex plugin marketplace list
codex plugin list --marketplace cw-development --available
codex plugin add cw@cw-development
```

For a Git-backed evaluation, pin a full immutable commit SHA and include both
sparse paths:

```text
codex plugin marketplace add Queopius/cw \
  --ref <IMMUTABLE_COMMIT_SHA> \
  --sparse .agents/plugins \
  --sparse plugins/cw
```

Never document `dev` or another mutable branch as a stable Plugin channel.
The CLI does not install ZIP files directly; a candidate must be verified and
safely expanded into a temporary local marketplace first. The source checkout
guide at `docs/plugin-installation.md` records prerequisites, update, removal,
rollback, and cleanup boundaries.

Remove the installed Plugin and marketplace source separately:

```text
codex plugin remove cw@cw-development
codex plugin marketplace remove cw-development
```

CLI `0.148.0` has no `plugin disable` command. Removing a marketplace source is
not Plugin removal and never authorizes deletion of a project or `.cw`.
