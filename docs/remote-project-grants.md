# CW Remote project grants

A project grant binds an opaque handle to all of:

- authenticated principal;
- workspace/tenant;
- paired device;
- canonical initialized local CW project.

The gateway stores the opaque handle and sanitized display identity. The local
agent alone stores the canonical path mapping and revalidates the principal,
workspace, device, allowed root, and initialized CW identity on every request.
The caller never chooses a path.

Grant creation requires an explicit local `cw remote grant --project …`
operation after pairing. Unknown, guessed, cross-tenant, cross-device, or
revoked handles fail closed with a typed grant/scope error. OAuth/session,
device, and individual project grants can be revoked independently.

No global `handle -> device` lookup is authoritative: every persistence query
is tenant/principal scoped and the local binding is checked again before
`CWApplication` is invoked.
