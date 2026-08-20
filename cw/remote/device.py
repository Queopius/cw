from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cw.core.utils import utc_now

from .errors import RemoteError, RemoteErrorCode
from .persistence import DeviceRecord, RemoteStore


_DEVICE_ID = re.compile(r"cwd_[A-Za-z0-9_-]{20,96}")


def _crypto() -> tuple[Any, Any, Any, Any]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - optional boundary
        raise RuntimeError("CW Remote requires codex-workflow[remote]") from exc
    return serialization, Ed25519PrivateKey, Ed25519PublicKey, base64.urlsafe_b64encode


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device request timestamp is invalid") from exc


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    device_id: str
    private_key: str
    public_key: str
    created_at: str
    schema_version: int = 1

    @classmethod
    def generate(cls) -> "DeviceCredential":
        serialization, PrivateKey, _, _ = _crypto()
        key = PrivateKey.generate()
        private = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return cls(
            device_id="cwd_" + _b64(secrets.token_bytes(18)),
            private_key=_b64(private),
            public_key=_b64(public),
            created_at=utc_now(),
        )

    def __post_init__(self) -> None:
        if _DEVICE_ID.fullmatch(self.device_id) is None:
            raise ValueError("Device identifier is invalid")
        if len(_unb64(self.private_key)) != 32 or len(_unb64(self.public_key)) != 32:
            raise ValueError("Device key material is invalid")

    def sign(self, message: bytes) -> str:
        _, PrivateKey, _, _ = _crypto()
        return _b64(PrivateKey.from_private_bytes(_unb64(self.private_key)).sign(message))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            payload = json.dumps({
                "schema_version": self.schema_version,
                "device_id": self.device_id,
                "private_key": self.private_key,
                "public_key": self.public_key,
                "created_at": self.created_at,
            }, sort_keys=True, separators=(",", ":"))
            os.write(descriptor, payload.encode("utf-8"))
        finally:
            os.close(descriptor)
        if os.name != "nt":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    @classmethod
    def load(cls, path: Path) -> "DeviceCredential":
        if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device credential permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "device_id", "private_key", "public_key", "created_at",
        } or payload.get("schema_version") != 1:
            raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device credential is invalid")
        return cls(**payload)


def device_signature_message(
    *, method: str, path: str, body: bytes, timestamp: str, nonce: str,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join((method.upper(), path, digest, timestamp, nonce)).encode("utf-8")


def verify_device_signature(
    store: RemoteStore, *, device_id: str, method: str, path: str, body: bytes,
    timestamp: str, nonce: str, signature: str, maximum_skew_seconds: int = 60,
) -> DeviceRecord:
    record = store.device(device_id)
    if record is None:
        raise RemoteError(RemoteErrorCode.DEVICE_NOT_PAIRED, "Device is not paired", http_status=401)
    if record.revoked_at is not None:
        raise RemoteError(RemoteErrorCode.DEVICE_REVOKED, "Device is revoked", http_status=403)
    now = datetime.now(timezone.utc)
    request_time = _parse_time(timestamp)
    if abs((now - request_time).total_seconds()) > maximum_skew_seconds:
        raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device request timestamp is stale", http_status=401)
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", nonce):
        raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device nonce is invalid", http_status=401)
    try:
        _, _, PublicKey, _ = _crypto()
        PublicKey.from_public_bytes(_unb64(record.public_key)).verify(
            _unb64(signature),
            device_signature_message(
                method=method, path=path, body=body, timestamp=timestamp, nonce=nonce,
            ),
        )
    except Exception as exc:
        raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Device signature is invalid", http_status=401) from exc
    store.use_device_nonce(device_id, nonce, timestamp)
    store.touch_device(device_id, utc_now())
    return record


@dataclass(frozen=True, slots=True)
class PairingChallenge:
    challenge_id: str
    user_code: str
    device_id: str
    display_name: str
    expires_at: str


class PairingService:
    def __init__(self, store: RemoteStore, *, lifetime_seconds: int = 300) -> None:
        self.store = store
        self.lifetime_seconds = lifetime_seconds

    @staticmethod
    def _hash_code(challenge_id: str, code: str) -> str:
        return hashlib.sha256(f"{challenge_id}:{code}".encode("utf-8")).hexdigest()

    def request(self, credential: DeviceCredential, display_name: str) -> PairingChallenge:
        return self.request_public(
            device_id=credential.device_id,
            public_key=credential.public_key,
            display_name=display_name,
        )

    def request_public(
        self, *, device_id: str, public_key: str, display_name: str,
    ) -> PairingChallenge:
        if not display_name or len(display_name) > 80:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Device display name is invalid")
        if _DEVICE_ID.fullmatch(device_id) is None or len(_unb64(public_key)) != 32:
            raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Device public identity is invalid")
        challenge_id = "pair_" + _b64(secrets.token_bytes(18))
        user_code = "-".join((secrets.token_hex(2).upper(), secrets.token_hex(2).upper()))
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expires = now + timedelta(seconds=self.lifetime_seconds)
        self.store.create_pairing_challenge(
            challenge_id=challenge_id,
            code_hash=self._hash_code(challenge_id, user_code),
            device_id=device_id,
            public_key=public_key,
            display_name=display_name,
            created_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )
        self.store.audit("device_pair_requested", outcome="PENDING", device_id=device_id)
        return PairingChallenge(
            challenge_id, user_code, device_id, display_name,
            expires.isoformat().replace("+00:00", "Z"),
        )

    def confirm(
        self, *, challenge_id: str, user_code: str, principal_id: str, workspace_id: str,
    ) -> DeviceRecord:
        record = self.store.confirm_pairing(
            challenge_id=challenge_id,
            code_hash=self._hash_code(challenge_id, user_code),
            principal_id=principal_id,
            workspace_id=workspace_id,
            confirmed_at=utc_now(),
        )
        self.store.audit(
            "device_paired", outcome="ALLOWED", device_id=record.device_id,
            principal_id=principal_id, workspace_id=workspace_id,
        )
        return record

    def pending_by_user_code(self, user_code: str) -> dict[str, Any]:
        normalized = user_code.strip().upper()
        for record in self.store.pending_pairing_challenges():
            if record["code_hash"] == self._hash_code(str(record["challenge_id"]), normalized):
                if record["expires_at"] <= utc_now():
                    raise RemoteError(RemoteErrorCode.AUTHORIZATION_REQUIRED, "Pairing challenge has expired")
                return record
        raise RemoteError(RemoteErrorCode.INVALID_REQUEST, "Pairing challenge is invalid")

    def reject(
        self, *, challenge_id: str, user_code: str, principal_id: str, workspace_id: str,
    ) -> None:
        self.store.reject_pairing(
            challenge_id=challenge_id,
            code_hash=self._hash_code(challenge_id, user_code),
            principal_id=principal_id,
            workspace_id=workspace_id,
            rejected_at=utc_now(),
        )
        self.store.audit(
            "device_pair_rejected", outcome="DENIED",
            principal_id=principal_id, workspace_id=workspace_id,
        )


def signed_headers(credential: DeviceCredential, *, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = utc_now()
    nonce = _b64(secrets.token_bytes(18))
    signature = credential.sign(device_signature_message(
        method=method, path=path, body=body, timestamp=timestamp, nonce=nonce,
    ))
    return {
        "x-cw-device-id": credential.device_id,
        "x-cw-timestamp": timestamp,
        "x-cw-nonce": nonce,
        "x-cw-signature": signature,
    }
