# CW Remote device pairing

Pairing is an explicit human ceremony:

1. `cw remote pair` creates an Ed25519 device key locally.
2. The gateway issues a short-lived random challenge and human-readable code.
3. The CLI prints the browser confirmation route, for example
   `https://staging-mcp.cwcli.dev/remote/pair`, and the short code.
4. The human opens the route, authenticates through OAuth, enters the code, and
   sees the requesting device name and sanitized device identifier.
5. Explicit approval binds the device public key to one principal and
   workspace. Explicit rejection consumes the code without pairing a device.
6. The code is consumed exactly once; only its salted hash was stored.
7. The local agent retains only its device credential.

Challenges expire, are single-use, and cannot become long-term credentials.
Pairing confirmation is auditable. Device revocation immediately rejects new
polls, grants, and responses. Rotation creates a replacement key/device binding
and revokes the old one; the gateway never receives a private device key.

Knowing a tunnel ID, project handle, or pairing challenge is not sufficient to
authorize a device. Pairing does not grant a repository: project authorization
is a separate local ceremony.

Opening the pairing page or completing OAuth login does not pair a device. The
browser flow requires a deliberate Approve or Reject action and uses a
short-lived, HttpOnly, signed session cookie. Bearer tokens are never copied
through the terminal, embedded in URLs, or rendered in page HTML.
