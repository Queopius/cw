# Agent output protocol

CW Core 0.16.0 adds an explicit presentation protocol for agents and automation.
It is a projection of the same canonical command result used by the human
renderer: choosing a format never changes preflight, authorization, mutation,
evidence, rollback, idempotency, or exit-code behavior.

## Choosing a format

```bash
cw status --output=human
cw status --output=json
cw history --output=jsonl
cw doctor --llm
```

| Selection | Intended use |
| --- | --- |
| `human` | Interactive terminal output. This remains the default, including through pipes and CI. |
| `json` | One minified `cw.output.v1` document. |
| `jsonl` | One `cw.output.v1` event or item per line; intermediate records use `partial`. |
| `--llm` | Stable alias for compact, quiet, bounded JSON. |
| legacy `--json` | The pre-0.16 raw JSON contract, retained for existing scripts. |

`--output=json` and legacy `--json` are deliberately different contracts. New
integrations should use the versioned output protocol. CW never selects LLM mode
from TTY detection; redirection does not change behavior.

Selection precedence is CLI, `CW_OUTPUT_MODE`, global `[output] mode`, then
`human`. Configure the global default with:

```bash
cw config set output.mode llm
```

An explicit `--output=human` overrides both environment and configuration.
Incompatible selectors fail with `USAGE_ERROR` and do not execute the command.

## `cw.output.v1`

Every result contains `schema`, `ok`, `command`, `status`, `changed`, and
`truncation`, followed by exactly one of `data` or `error`. `operation_id` and
`page` are present when applicable. Status is one of:

```text
success · noop · error · authorization_required · blocked · partial · cancelled
```

`noop` means a successful, legitimate idempotent replay. Missing authorization,
a failed precondition, or a command error is never rewritten as `noop`.

Governed output retains repository/PR identity, head and base branches and
SHAs, CAS hashes, evidence schema and generation, authorization state, final
state, and the next safe action whenever the canonical result contains them.
These values cannot be removed through field projection or truncation.

Discover the runtime contract without copying human help into every response:

```bash
cw capabilities --output=json
cw schema show cw.output.v1 --output=json
```

## stdout, stderr, and exit codes

In versioned JSON, JSONL, and LLM modes, stdout contains only the requested
protocol. It contains no ANSI, banners, spinner output, progress bars, or logs.
stderr is empty unless `--debug` is explicitly selected or formatter
initialization itself fails. Each JSONL line is independently valid JSON.

Exit codes retain their command meaning: `0` for success or legitimate noop,
`1` for operational failure, `2` for invalid usage/configuration, `3` for a
human or governed blocker, command-specific codes such as CAS exit `4`, and
`130` for cancellation.

## Compact errors and debug

Machine errors expose a stable code, one-sentence message, `retryable`, a narrow
hint, and a deterministic correlation ID. Secrets, credential-like values,
private home paths, and runtime roots are redacted. Remote HTML and stack traces
are not returned.

`--debug` writes only expanded, redacted diagnostics to stderr. It never mixes
a traceback into the JSON document and does not relax secret filtering.

## Field selection

Supported read commands accept a comma-separated allowlist:

```bash
cw status --output=json --fields=state,phase
cw capabilities --llm --fields=core,output.pagination.maximum_limit
```

Nested paths use dot notation. Unknown fields, indexing, wildcards, expressions,
and fields unsupported by a command fail closed. Selecting fields changes only
`data`; envelope and governance invariants remain present. Use
`cw capabilities --output=json` to discover support.

## Pagination and large results

Supported listings accept `--limit`, opaque `--cursor`, and bounded `--all`.
LLM mode defaults to 10 items and the maximum page size is 100. Stable ordering,
`has_more`, `next_cursor`, and explicit truncation metadata prevent silent loss.
Invalid or stale cursors fail with `USAGE_ERROR`. JSON mode remains unbounded
unless pagination is requested explicitly; use it when the complete canonical
payload is required.

Large evidence remains authoritative on disk. Compact results may expose its
identifier, safe reference, SHA-256, byte size, and explicit truncation state;
CW never truncates authorizations, invalidations, CAS hashes, conflicts,
integrity failures, or branch-protection changes.

Use `--expand` when the complete canonical data is required. Expansion never
changes execution and does not silently imply `--all`; pagination remains an
independent explicit bound. Any default LLM projection sets
`truncation.reason=llm_projection` rather than pretending the compact view is
the complete evidence.

## CI and agent examples

```bash
# CI: stable one-document protocol and explicit projection
cw status --output=json --fields=state,phase,ready

# Agent: compact health decision; passing and neutral doctor details are omitted
cw doctor --llm

# Agent: bounded history continuation
cw history --llm --limit=10
cw history --llm --cursor='<opaque cursor>'
```

The local MCP adapter remains a separate, governed transport. Core 0.16.0 does
not change the 12-tool Plugin 0.1.0 registry or `cw.remote.v1`. MCP hosts should
consume typed `structuredContent`; tool annotations are advisory and never
replace server-side authorization. See the official
[OpenAI Plugins reference](https://developers.openai.com/plugins/reference).
