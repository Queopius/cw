# CW Remote device pairing

Pairing is an explicit human ceremony:

1. `cw remote pair` creates an Ed25519 device key locally.
2. The gateway issues a short-lived random challenge and human-readable code.
3. The human authenticates to the remote service and sees the requesting
   device name.
4. Confirmation binds the device public key to one principal and workspace.
5. The code is consumed exactly once; only its salted hash was stored.
6. The local agent retains only its device credential.

Challenges expire, are single-use, and cannot become long-term credentials.
Pairing confirmation is auditable. Device revocation immediately rejects new
polls, grants, and responses. Rotation creates a replacement key/device binding
and revokes the old one; the gateway never receives a private device key.

Knowing a tunnel ID, project handle, or pairing challenge is not sufficient to
authorize a device. Pairing does not grant a repository: project authorization
is a separate local ceremony.
