# Local agent staging runbook

Copy `config/staging-agent.example.env` to a private location and replace only
local placeholders. Do not commit the resulting file.

1. Pair one device:

   ```text
   cw remote pair --gateway-url https://staging-mcp.cwcli.dev \
     --credentials <private-device-file> --device-name "CW staging agent"
   ```

2. Confirm the displayed short-lived code in the authenticated staging pairing
   flow.
3. Grant exactly one initialized project and use the same project as the
   allowed root:

   ```text
   cw remote grant --gateway-url https://staging-mcp.cwcli.dev \
     --credentials <private-device-file> --state <private-state-file> \
     --project <authorized-project> --allowed-root <authorized-project>
   ```

4. Start the outbound-only agent with the same private files and root. Verify
   `agent_connected` without disclosing the local path remotely.

Stop with the normal process signal. A disconnect does not fabricate workflow
failure. On a lost key, revoke the device before deleting local credentials,
then pair a new device and regrant projects. Never grant an entire user home or
accept a project path from the gateway.
