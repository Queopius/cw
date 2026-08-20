# ADR 0006: Gateway-to-agent transport

**Status:** Accepted for the CW 0.13 candidate

## Decision

Use versioned HTTPS long polling initiated by the local CW agent for
`cw.remote.v1`. Requests and responses use closed typed schemas and signed
device messages.

## Rationale

Long polling needs no inbound workstation listener, works through common
enterprise proxies/firewalls, has straightforward cross-platform clients,
and makes reconnect/duplicate delivery deterministic. WebSockets offer lower
idle latency but add proxy and lifecycle complexity not justified by the
bounded CW operation rate. Arbitrary JSON-RPC proxying was rejected because it
would bypass the governed registry.

The tradeoff is one extra request cycle and moderate polling latency. Protocol
versioning allows a later compatible transport without moving workflow logic
into the gateway.
