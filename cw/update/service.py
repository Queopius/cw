from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cw import __version__
from cw.core.errors import CwError, ErrorCode

from .cache import UpdateCache
from .config import UpdateSettings, load_update_settings
from .installation import InstallResult, ManagedInstallation
from .models import ReleaseManifest, UpdateInfo, Version
from .provider import Downloader, GitHubReleaseProvider, HttpsDownloader, ReleaseProvider


@dataclass(slots=True)
class UpdateService:
    provider: ReleaseProvider
    downloader: Downloader
    installation: ManagedInstallation
    cache: UpdateCache
    settings: UpdateSettings

    @classmethod
    def default(cls) -> "UpdateService":
        return cls(
            provider=GitHubReleaseProvider(), downloader=HttpsDownloader(),
            installation=ManagedInstallation(), cache=UpdateCache(),
            settings=load_update_settings(),
        )

    @property
    def installed(self) -> Version:
        return Version.parse(self.installation.active_version())

    def check(self, *, force: bool = False) -> UpdateInfo:
        if not force and self.cache.fresh(self.settings.channel, self.settings.check_interval_hours):
            cached = self.cache.info(self.installed)
            if cached is not None:
                return cached
        manifest = self.provider.latest(self.settings.channel)
        self._validate_channel(manifest)
        self.cache.store(manifest, self.installed)
        return UpdateInfo(self.installed, manifest.version, manifest.channel, manifest)

    def cached_notice(self) -> UpdateInfo | None:
        if not self.settings.check or _is_ci() or not self.installation.managed:
            return None
        if self.cache.fresh(self.settings.channel, self.settings.check_interval_hours):
            info = self.cache.info(self.installed)
            return info if info and info.available else None
        try:
            info = self.check(force=True)
        except CwError:
            self.cache.store_failure(self.settings.channel)
            return None
        return info if info.available else None

    def info(self, *, force: bool = False) -> UpdateInfo:
        return self.check(force=force)

    def install(
        self, *, requested_version: str | None = None, with_remote: bool = False,
    ) -> tuple[UpdateInfo, InstallResult | None]:
        if not self.installation.managed:
            raise CwError(
                "Development installation detected",
                ErrorCode.UPDATE_DEVELOPMENT_INSTALL,
                details="Self-update is disabled for editable/source installations.",
                exit_code=3,
            )
        if requested_version is not None:
            requested = Version.parse(requested_version)
            manifest = self.provider.get(str(requested), self.settings.channel)
            self._validate_channel(manifest)
            info = UpdateInfo(self.installed, manifest.version, manifest.channel, manifest)
        else:
            info = self.check(force=True)
        if not info.available and requested_version is None and not with_remote:
            return info, None
        artifact = info.manifest.artifact_for_current_platform()
        self.installation.paths.share.mkdir(parents=True, exist_ok=True)
        with self.installation.locked():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".cw-download-", suffix=".tar.gz", dir=self.installation.paths.share,
            )
            os.close(descriptor)
            try:
                from pathlib import Path
                archive = Path(temporary_name)
                self.downloader.download(artifact.url, archive)
                result = self.installation.install_release(
                    info.manifest, artifact, archive,
                    allow_downgrade=requested_version is not None,
                    already_locked=True,
                    runtime_features_requested=(
                        {*self.installation.active_features(), "remote"}
                        if with_remote else None
                    ),
                )
            finally:
                from pathlib import Path
                Path(temporary_name).unlink(missing_ok=True)
        return info, result

    def rollback(self) -> InstallResult:
        return self.installation.rollback()

    def _validate_channel(self, manifest: ReleaseManifest) -> None:
        if manifest.channel != self.settings.channel:
            raise CwError("Release channel does not match configured channel", ErrorCode.UPDATE_MANIFEST_ERROR)
        if self.settings.channel == "stable" and manifest.version.is_prerelease:
            raise CwError("Stable channel cannot install a prerelease", ErrorCode.UPDATE_MANIFEST_ERROR)


def automatic_update_notice() -> UpdateInfo | None:
    try:
        return UpdateService.default().cached_notice()
    except Exception:
        return None


def _is_ci() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}
