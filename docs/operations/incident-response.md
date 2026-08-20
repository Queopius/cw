# Staging incident response

Contain first; investigate second.

| Incident | Immediate containment |
|---|---|
| OAuth client credential exposed | Revoke/rotate in Auth0 and disconnect the ChatGPT connection |
| Bearer token exposed | Revoke the user/client grant, locally deny known token ID, shorten exposure by expiry |
| Device compromised or key lost | Revoke device; this also revokes its project grants; stop the agent |
| Project handle exposed | Revoke and recreate that project grant |
| Suspected tenant crossover | Disable staging ingress, preserve sanitized audit evidence, revoke affected principals/devices |
| Gateway compromise | Stop Render service, revoke Auth0 integration, rotate provider/database access, restore trusted artifact |
| Database compromise | Stop gateway, revoke all devices/tokens, preserve evidence, restore only after scope review |

Never paste tokens, private keys, raw database contents, source, or raw `.cw`
into an incident ticket. Record timestamps, correlation identifiers, affected
opaque handles, exact SHA, containment, and validation evidence. Human gate or
extension approval remains invalid unless performed through a future distinct
authorization ceremony.
