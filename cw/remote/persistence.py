from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cw.core.utils import utc_now

from .errors import RemoteError, RemoteErrorCode


SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    device_id: str
    principal_id: str
    workspace_id: str
    public_key: str
    display_name: str
    created_at: str
    last_seen_at: str | None
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class ProjectGrantRecord:
    project_handle: str
    principal_id: str
    workspace_id: str
    device_id: str
    display_name: str
    created_at: str
    revoked_at: str | None


class RemoteStore:
    """Transactional minimum-metadata store for the remote control plane.

    SQLite is the deterministic reference backend.  The public services depend
    on this small repository interface rather than SQL outside this module, so
    another transactional backend can be supplied for a real deployment.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if self.path != ":memory:" and os.name != "nt":
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            connection = self._connection
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            versions = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
            if 1 not in versions:
                connection.executescript(
                    """
                    CREATE TABLE pairing_challenges (
                        challenge_id TEXT PRIMARY KEY,
                        code_hash TEXT NOT NULL UNIQUE,
                        device_id TEXT NOT NULL,
                        public_key TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        confirmed_at TEXT,
                        consumed_at TEXT,
                        principal_id TEXT,
                        workspace_id TEXT
                    );
                    CREATE TABLE devices (
                        device_id TEXT PRIMARY KEY,
                        principal_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        public_key TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT,
                        revoked_at TEXT,
                        UNIQUE (workspace_id, public_key)
                    );
                    CREATE TABLE device_nonces (
                        device_id TEXT NOT NULL,
                        nonce TEXT NOT NULL,
                        used_at TEXT NOT NULL,
                        PRIMARY KEY (device_id, nonce),
                        FOREIGN KEY (device_id) REFERENCES devices(device_id)
                    );
                    CREATE TABLE project_grants (
                        project_handle TEXT PRIMARY KEY,
                        principal_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        revoked_at TEXT,
                        FOREIGN KEY (device_id) REFERENCES devices(device_id),
                        UNIQUE (workspace_id, device_id, project_handle)
                    );
                    CREATE INDEX project_grants_scope_idx
                        ON project_grants (workspace_id, principal_id, device_id, project_handle);
                    CREATE TABLE routed_requests (
                        workspace_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        principal_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        project_handle TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, request_id),
                        FOREIGN KEY (device_id) REFERENCES devices(device_id),
                        FOREIGN KEY (project_handle) REFERENCES project_grants(project_handle)
                    );
                    CREATE TABLE revoked_tokens (
                        issuer TEXT NOT NULL,
                        token_id TEXT NOT NULL,
                        revoked_at TEXT NOT NULL,
                        PRIMARY KEY (issuer, token_id)
                    );
                    CREATE TABLE audit_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        occurred_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        request_id TEXT,
                        operation_id TEXT,
                        principal_id TEXT,
                        workspace_id TEXT,
                        device_id TEXT,
                        project_handle TEXT,
                        capability TEXT,
                        actor TEXT,
                        origin TEXT,
                        outcome TEXT NOT NULL,
                        detail_json TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, utc_now()),
                )

    def schema_version(self) -> int:
        row = self._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def create_pairing_challenge(
        self, *, challenge_id: str, code_hash: str, device_id: str, public_key: str,
        display_name: str, created_at: str, expires_at: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO pairing_challenges(
                    challenge_id, code_hash, device_id, public_key, display_name,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (challenge_id, code_hash, device_id, public_key, display_name, created_at, expires_at),
            )

    def pairing_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM pairing_challenges WHERE challenge_id = ?", (challenge_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def confirm_pairing(
        self, *, challenge_id: str, code_hash: str, principal_id: str, workspace_id: str,
        confirmed_at: str,
    ) -> DeviceRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pairing_challenges WHERE challenge_id = ? AND code_hash = ?",
                (challenge_id, code_hash),
            ).fetchone()
            if row is None:
                raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Pairing challenge is invalid")
            if row["consumed_at"] is not None or row["confirmed_at"] is not None:
                raise RemoteError(RemoteErrorCode.OPERATION_CONFLICT, "Pairing challenge was already used")
            if row["expires_at"] <= confirmed_at:
                raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Pairing challenge has expired")
            connection.execute(
                """UPDATE pairing_challenges SET confirmed_at = ?, consumed_at = ?,
                    principal_id = ?, workspace_id = ? WHERE challenge_id = ?""",
                (confirmed_at, confirmed_at, principal_id, workspace_id, challenge_id),
            )
            connection.execute(
                """INSERT INTO devices(
                    device_id, principal_id, workspace_id, public_key, display_name,
                    created_at, last_seen_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (
                    row["device_id"], principal_id, workspace_id, row["public_key"],
                    row["display_name"], confirmed_at,
                ),
            )
        result = self.device(str(row["device_id"]))
        assert result is not None
        return result

    def device(self, device_id: str) -> DeviceRecord | None:
        row = self._connection.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return DeviceRecord(**dict(row)) if row is not None else None

    def touch_device(self, device_id: str, at: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE devices SET last_seen_at = ? WHERE device_id = ?", (at, device_id))

    def revoke_device(self, device_id: str, at: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE devices SET revoked_at = ? WHERE device_id = ?", (at, device_id))
            connection.execute(
                "UPDATE project_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE device_id = ?",
                (at, device_id),
            )

    def use_device_nonce(self, device_id: str, nonce: str, used_at: str) -> None:
        try:
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO device_nonces(device_id, nonce, used_at) VALUES (?, ?, ?)",
                    (device_id, nonce, used_at),
                )
        except sqlite3.IntegrityError as exc:
            raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device request replay was rejected") from exc

    def create_project_grant(
        self, *, project_handle: str, principal_id: str, workspace_id: str,
        device_id: str, display_name: str, created_at: str,
    ) -> ProjectGrantRecord:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO project_grants(
                    project_handle, principal_id, workspace_id, device_id,
                    display_name, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (project_handle, principal_id, workspace_id, device_id, display_name, created_at),
            )
        record = self.project_grant(project_handle)
        assert record is not None
        return record

    def project_grant(self, project_handle: str) -> ProjectGrantRecord | None:
        row = self._connection.execute(
            "SELECT * FROM project_grants WHERE project_handle = ?", (project_handle,),
        ).fetchone()
        return ProjectGrantRecord(**dict(row)) if row is not None else None

    def resolve_project_grant(
        self, *, project_handle: str, principal_id: str, workspace_id: str,
    ) -> ProjectGrantRecord:
        record = self.project_grant(project_handle)
        # Deliberately do not distinguish an unknown handle from another
        # tenant's handle at the public boundary.
        if (
            record is None
            or record.principal_id != principal_id
            or record.workspace_id != workspace_id
            or record.revoked_at is not None
        ):
            raise RemoteError(
                RemoteErrorCode.PROJECT_NOT_GRANTED,
                "Project is not granted to this principal and workspace",
                http_status=403,
            )
        device = self.device(record.device_id)
        if device is None or device.revoked_at is not None:
            raise RemoteError(RemoteErrorCode.DEVICE_REVOKED, "The paired device is revoked", http_status=403)
        return record

    def revoke_project_grant(self, project_handle: str, at: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE project_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE project_handle = ?",
                (at, project_handle),
            )

    def record_routed_request(
        self, *, workspace_id: str, request_id: str, principal_id: str,
        device_id: str, project_handle: str, operation_id: str, tool: str,
        request_digest: str, state: str, at: str,
    ) -> bool:
        try:
            with self.transaction() as connection:
                connection.execute(
                    """INSERT INTO routed_requests(
                        workspace_id, request_id, principal_id, device_id,
                        project_handle, operation_id, tool, request_digest,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id, request_id, principal_id, device_id,
                        project_handle, operation_id, tool, request_digest,
                        state, at, at,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            row = self._connection.execute(
                "SELECT * FROM routed_requests WHERE workspace_id = ? AND request_id = ?",
                (workspace_id, request_id),
            ).fetchone()
            if row is None or any((
                row["principal_id"] != principal_id,
                row["device_id"] != device_id,
                row["project_handle"] != project_handle,
                row["operation_id"] != operation_id,
                row["tool"] != tool,
                row["request_digest"] != request_digest,
            )):
                raise RemoteError(
                    RemoteErrorCode.OPERATION_CONFLICT,
                    "request_id was already used for a different remote request",
                    http_status=409,
                )
            return False

    def routed_request(self, workspace_id: str, request_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM routed_requests WHERE workspace_id = ? AND request_id = ?",
            (workspace_id, request_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_routed_state(self, workspace_id: str, request_id: str, state: str, at: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE routed_requests SET state = ?, updated_at = ? WHERE workspace_id = ? AND request_id = ?",
                (state, at, workspace_id, request_id),
            )

    def revoke_token(self, issuer: str, token_id: str, at: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO revoked_tokens(issuer, token_id, revoked_at) VALUES (?, ?, ?)",
                (issuer, token_id, at),
            )

    def token_revoked(self, issuer: str, token_id: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM revoked_tokens WHERE issuer = ? AND token_id = ?",
            (issuer, token_id),
        ).fetchone() is not None

    def audit(self, event_type: str, *, outcome: str, detail: dict[str, Any] | None = None, **fields: Any) -> None:
        permitted = {
            "request_id", "operation_id", "principal_id", "workspace_id",
            "device_id", "project_handle", "capability", "actor", "origin",
        }
        unknown = set(fields) - permitted
        if unknown:
            raise ValueError(f"Unsupported audit fields: {', '.join(sorted(unknown))}")
        values = {name: fields.get(name) for name in permitted}
        safe_detail = detail or {}
        encoded = json.dumps(safe_detail, sort_keys=True, separators=(",", ":"))
        if any(marker in encoded.lower() for marker in ("token", "secret", "password", "source")):
            encoded = "{}"
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO audit_events(
                    occurred_at, event_type, request_id, operation_id, principal_id,
                    workspace_id, device_id, project_handle, capability, actor,
                    origin, outcome, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    utc_now(), event_type, values["request_id"], values["operation_id"],
                    values["principal_id"], values["workspace_id"], values["device_id"],
                    values["project_handle"], values["capability"], values["actor"],
                    values["origin"], outcome, encoded,
                ),
            )

    def audit_events(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute("SELECT * FROM audit_events ORDER BY sequence")]

    def close(self) -> None:
        self._connection.close()
