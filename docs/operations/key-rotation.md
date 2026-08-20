# Staging key and credential rotation

- **Auth0 signing keys:** rely on JWKS `kid` rotation and cached HTTPS refresh;
  verify old/new overlap and rejection after retirement.
- **Auth0 client registration:** rotate or recreate in Auth0, update only the
  ChatGPT connection, and revoke the previous client. The CW gateway stores no
  client secret.
- **Render/provider access:** rotate in the provider control plane. No provider
  credential belongs in the image or repository.
- **Database access:** the SQLite disk has no shared password. Restrict Render
  account/service access and rotate provider access after compromise.
- **Device Ed25519 key:** revoke the device, create a new local credential,
  repeat explicit pairing, and regrant projects. Never transfer the old private
  key through the gateway.

After every rotation, verify authentication denial for the retired identity,
successful least-privilege access for the replacement, and unchanged
high-consequence denial.
