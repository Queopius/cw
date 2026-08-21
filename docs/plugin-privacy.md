# Plugin candidate privacy and data handling

**DRAFT — REQUIRES HUMAN AND LEGAL REVIEW**

This document describes technical data handling for the CW plugin candidate.
It is not a published Privacy Policy and must not be linked as a final legal or
submission document.

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

The staging gateway and OAuth discovery are implemented for testing. The
staging relay may process the normalized envelopes and minimum identity,
device, grant, routing, revocation, and audit metadata described below; it does
not make the service production or authorize source upload. No production CW
remote service is deployed. Production requires a separately approved policy
covering identity, authentication, data minimization, retention, deletion,
logging, incident response, and subprocess exposure.

## User control

Operators choose which canonical project roots are authorized and can stop the
tunnel/client, revoke its runtime key or workspace association, and disable or
remove the plugin. Project initialization is not automatic. The
plugin cannot authorize completion extensions or perform administrative
repair, release, deployment, or update actions.

## Public-policy gap

Before public submission, Fantomid LLC must approve final legal language, a
public privacy contact, retention/deletion commitments for any production
remote runtime, and the relationship between the Apache-2.0 software license
and service terms.

## CW 0.12 production data-flow decision

The selected gateway/relay model keeps repository access in the paired local
agent. The following is the technical minimum-disclosure contract, not legal
marketing language:

| Data category | Local stdio | Secure MCP Tunnel development | Production gateway/relay |
| --- | --- | --- | --- |
| Repository source and file contents | Local only | Does not cross by default | Local only; no generic source tool |
| Git diff and credentials | Local only | Does not cross by default | Local only |
| Raw `.cw` files | Local only | Not uploaded | Local only |
| Normalized phase/gate/completion summaries | Local client | Crosses tunnel when requested | Crosses relay when requested |
| Sanitized validation/review result | Local client | Crosses tunnel when requested | Crosses relay when requested |
| Raw validation/reviewer logs | Local only | Not returned | Local only; evidence reference only |
| User identity | Local OS context | OpenAI development connection | CW principal/workspace token claims |
| Project identity | Canonical local path internally | Opaque handle externally | Opaque project/device handles |
| Audit/correlation metadata | Local evidence | Tunnel/platform policy applies | Minimum request/operation/capability outcome |

Production storage may include account, device, project-grant, revocation,
routing, and minimum audit metadata. It must not include source or complete
`.cw` evidence by default. Final retention, deletion, subprocessors, regions,
and data-subject commitments require legal/business approval before submission.
