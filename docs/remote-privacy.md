# CW Remote privacy and data flow

By default, CW repositories remain on the user's machine. Raw `.cw` state also
remains local. The `cw.remote.v1` implementation introduced in Core 0.13 and
retained by Core 0.15.0 does not implement repository upload or remote workflow
storage.

| Data category | Leaves device? | Persisted by gateway? |
|---|---:|---:|
| repository source/files/diff | no | no |
| raw `.cw` documents/evidence | no | no |
| absolute paths/home directory | no | no |
| Git/environment/credentials | no | no |
| normalized project/phase/gate/completion status | when requested | no response body persistence |
| bounded validation/review summaries | when requested | no response body persistence |
| principal/workspace/device IDs | yes | yes |
| opaque project handle/display name | yes | yes |
| request/operation IDs, tool, digest, state | yes | minimum routing/audit metadata |
| OAuth/device secrets | bearer token in transit only; device private key no | no |

The gateway routes normalized results to the authenticated MCP client, so
those requested summaries cross the network. The reference store records only
minimum identity, grant, routing digest/state, revocation, and sanitized audit
metadata. It deliberately has no table for repository source, raw `.cw`, or
tool response content.

Branch names and workflow labels may be included when present in a normalized
CW result. Absolute paths, secret-shaped values, environment variables,
unrestricted logs, and reviewer hidden reasoning are removed before transport.
