from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cw.core.utils import atomic_json, load_json, utc_now

from .config import update_cache_path
from .models import ReleaseManifest, UpdateInfo, Version


@dataclass(slots=True)
class UpdateCache:
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = update_cache_path()

    def read(self) -> dict[str, Any] | None:
        assert self.path is not None
        if not self.path.is_file():
            return None
        try:
            value = load_json(self.path)
        except Exception:
            return None
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else None

    def fresh(self, channel: str, interval_hours: int) -> bool:
        value = self.read()
        if not value or value.get("channel") != channel:
            return False
        try:
            checked = datetime.fromisoformat(str(value["last_checked_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return False
        return datetime.now(timezone.utc) - checked < timedelta(hours=interval_hours)

    def store(self, manifest: ReleaseManifest, installed: Version) -> None:
        assert self.path is not None
        atomic_json(self.path, {
            "schema_version": 1,
            "channel": manifest.channel,
            "last_checked_at": utc_now(),
            "latest_version": str(manifest.version),
            "installed_version": str(installed),
            "release_url": manifest.release_url,
            "update_available": manifest.version > installed,
            "manifest": _manifest_dict(manifest),
        })

    def store_failure(self, channel: str) -> None:
        """Cache only failure timing; never persist transport bodies."""
        assert self.path is not None
        atomic_json(self.path, {
            "schema_version": 1,
            "channel": channel,
            "last_checked_at": utc_now(),
            "update_available": False,
            "check_failed": True,
        })

    def info(self, installed: Version) -> UpdateInfo | None:
        value = self.read()
        if not value or not isinstance(value.get("manifest"), dict):
            return None
        try:
            manifest = ReleaseManifest.from_dict(value["manifest"])
        except Exception:
            return None
        return UpdateInfo(installed, manifest.version, manifest.channel, manifest)


def _manifest_dict(manifest: ReleaseManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "version": str(manifest.version),
        "channel": manifest.channel,
        "published_at": manifest.published_at,
        "minimum_project_schema": manifest.minimum_project_schema,
        "maximum_project_schema": manifest.maximum_project_schema,
        "artifacts": [
            {
                "platform": item.platform, "arch": item.arch, "url": item.url,
                "sha256": item.sha256, "filename": item.filename,
            }
            for item in manifest.artifacts
        ],
        "release_notes": {"summary": manifest.summary, "url": manifest.release_url},
        **({"signature": manifest.signature} if manifest.signature is not None else {}),
    }
