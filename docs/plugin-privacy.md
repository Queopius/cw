# Plugin candidate privacy and data handling

This document describes the local CW plugin candidate. It is an engineering
draft, not a substitute for a lawyer-approved public privacy policy.

## Local candidate data flow

The local stdio process reads an operator-authorized, already initialized CW
project. It returns normalized workflow state, opaque project identity, phase
status, validated gates, Completion Contract status, blockers, and sanitized
operation results. Controlled actions write only normal `.cw` runtime and
evidence artifacts through `CWApplication`.

The candidate does not automatically expose source files, `.env`, process
environment variables, credentials, raw local paths, unrestricted logs,
reviewer prompts, or private model reasoning. Existing CW redaction removes
private paths and secret-shaped values from MCP results and diagnostics.

## Storage and transmission

CW stores project evidence locally in `.cw` under the user's repository. The
plugin adds no separate state database, telemetry service, account, or cloud
storage. In local Codex/desktop mode, stdio traffic stays on the local host.
In ChatGPT development mode, Secure MCP Tunnel transports MCP method/arguments
and normalized CW results through OpenAI's tunnel endpoint; CW does not send
the repository or create a remote state copy. CW does not define retention or
deletion beyond the user's local repository/evidence management and the
app/tunnel policies of the OpenAI workspace used for development.

No source upload or permanent CW remote service exists in this milestone. A
future public bridge would require a separately approved policy covering identity,
authentication, data minimization, retention, deletion, logging, incident
response, and subprocess exposure.

## User control

Operators choose which canonical project roots are authorized and can stop the
tunnel/client, revoke its runtime key or workspace association, and disable or
remove the plugin. Project initialization is not automatic. The
plugin cannot authorize completion extensions or perform administrative
repair, release, deployment, or update actions.

## Public-policy gap

Before public submission, Queopius must approve final legal language, a public
privacy contact, retention/deletion commitments for any remote runtime, and the
relationship between the Apache-2.0 software license and service terms.
