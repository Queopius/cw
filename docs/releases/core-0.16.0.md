# CW CLI v0.16.0

CW Core 0.16.0 introduces the versioned `cw.output.v1` Agent Output Protocol.
Agents and automation can explicitly choose minified JSON, JSONL, or bounded LLM
output while the default human interface remains unchanged.

The release adds deterministic typed errors, mandatory redaction, allowlisted
field selection, opaque cursor pagination, explicit truncation metadata, and
capability/schema discovery. Domain operations are format-neutral: gates,
confirmations, CAS hashes, evidence identity, idempotency, rollback, crash
recovery, and exit codes retain their existing semantics. Legacy `--json`
payloads remain available to existing scripts.

This is a Core-only release with exactly four Core assets. The public Plugin
remains `0.1.0` with its 12-tool contract unchanged. Remote protocol
`cw.remote.v1`, project schema `1`, and governance evidence schema `2` are
unchanged. Plugin metadata optimization is deliberately deferred to an
independent Plugin 0.2.0 wave.
