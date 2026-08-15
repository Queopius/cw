# Staging backup and restore

The Render disk receives provider snapshots, but CW also defines an explicit
logical staging procedure. It is prepared and **NOT EXERCISED**.

## Backup

1. Stop new staging connections and stop the gateway cleanly.
2. Confirm no gateway process has the SQLite database open.
3. Use Render's disk snapshot/export mechanism or an SQLite online-backup
   operation from an authorized maintenance process.
4. Encrypt the backup and store it in an approved restricted location.
5. Record schema version, exact gateway SHA, timestamp, checksum, and retention.

Do not use a live filesystem copy of the database. The backup contains minimum
principal/device/grant/routing/revocation/audit metadata, but no repository
source, raw `.cw`, device private key, or OAuth token.

## Restore exercise

Restore to an isolated staging disk, start the exact compatible image, verify
SQLite integrity and schema version, then test revocation, uniqueness,
idempotency, and a disposable agent pairing/grant. Do not reconnect real
agents until checks pass. Record the exercise separately; preparation is not a
PASS.
