from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from cw.core.errors import CwError, ErrorCode

from .models import ReleaseManifest


TRUSTED_RELEASE_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MANIFEST_ASSET = "cw-release-manifest.json"
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024


def _local_file_path(value: str, *, platform: str = os.name) -> str:
    """Convert a URL path to one native local path without weakening trust checks."""

    path = unquote(value)
    if platform == "nt":
        if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
            path = path[1:]
        return path.replace("/", "\\")
    return path


class ReleaseProvider(Protocol):
    def latest(self, channel: str) -> ReleaseManifest: ...
    def get(self, version: str, channel: str) -> ReleaseManifest: ...


class Downloader(Protocol):
    def download(self, url: str, destination: Path) -> None: ...


def require_trusted_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_RELEASE_HOSTS or parsed.username or parsed.password:
        raise CwError(
            "Release metadata references an untrusted origin",
            ErrorCode.UPDATE_MANIFEST_ERROR,
            "Run: cw error",
            details=url,
        )


@dataclass(slots=True)
class GitHubReleaseProvider:
    repository: str = "Queopius/cw"
    timeout: float = 3.0

    def latest(self, channel: str) -> ReleaseManifest:
        url = f"https://api.github.com/repos/{self.repository}/releases?per_page=30"
        releases = self._json(url)
        if not isinstance(releases, list):
            raise CwError("GitHub release response is invalid", ErrorCode.UPDATE_CHECK_ERROR)
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            prerelease = bool(release.get("prerelease"))
            tag = str(release.get("tag_name", ""))
            if channel == "stable" and prerelease:
                continue
            if channel == "beta" and not (prerelease and ("beta" in tag or "rc" in tag)):
                continue
            if channel == "dev" and not prerelease:
                continue
            manifest = self._manifest_from_release(release)
            if manifest is not None:
                return manifest
        raise CwError(
            f"No {channel} CW release manifest is available",
            ErrorCode.UPDATE_CHECK_ERROR,
        )

    def get(self, version: str, channel: str) -> ReleaseManifest:
        last_error: CwError | None = None
        for tag in (f"v{version}", version):
            try:
                release = self._json(f"https://api.github.com/repos/{self.repository}/releases/tags/{tag}")
            except CwError as exc:
                last_error = exc
                continue
            if isinstance(release, dict):
                manifest = self._manifest_from_release(release)
                if manifest is not None and str(manifest.version) == version and manifest.channel == channel:
                    return manifest
        raise CwError(
            f"CW {version} is not available on the {channel} channel",
            ErrorCode.UPDATE_CHECK_ERROR,
            details=last_error.details if last_error else None,
        )

    def _manifest_from_release(self, release: dict[str, Any]) -> ReleaseManifest | None:
        for asset in release.get("assets", []):
            if isinstance(asset, dict) and asset.get("name") == MANIFEST_ASSET:
                asset_url = asset.get("browser_download_url")
                if not isinstance(asset_url, str):
                    continue
                require_trusted_url(asset_url)
                return ReleaseManifest.from_dict(self._json(asset_url))
        return None

    def _json(self, url: str) -> Any:
        require_trusted_url(url)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "CW-Update-Client"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=ssl.create_default_context()) as response:
                final_url = response.geturl()
                require_trusted_url(final_url)
                return json.loads(response.read().decode("utf-8"))
        except CwError:
            raise
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CwError(
                "Could not check for CW updates",
                ErrorCode.UPDATE_CHECK_ERROR,
                details=str(exc),
            ) from exc


@dataclass(slots=True)
class HttpsDownloader:
    timeout: float = 30.0

    def download(self, url: str, destination: Path) -> None:
        require_trusted_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "CW-Update-Client"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=ssl.create_default_context()) as response:
                require_trusted_url(response.geturl())
                with destination.open("wb") as stream:
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise CwError("CW release download exceeds its safety limit", ErrorCode.UPDATE_DOWNLOAD_ERROR)
                        stream.write(chunk)
                    stream.flush()
        except CwError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise CwError(
                "Could not download the CW release",
                ErrorCode.UPDATE_DOWNLOAD_ERROR,
                "Run: cw update",
                details=str(exc),
            ) from exc


@dataclass(slots=True)
class LocalReleaseProvider:
    """Explicit test/development provider; never selected by the public CLI."""

    manifest_path: Path

    def latest(self, channel: str) -> ReleaseManifest:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CwError("Local release manifest is invalid", ErrorCode.UPDATE_MANIFEST_ERROR, details=str(exc)) from exc
        manifest = ReleaseManifest.from_dict(value)
        if manifest.channel != channel:
            raise CwError("Local release channel does not match", ErrorCode.UPDATE_MANIFEST_ERROR)
        return manifest

    def get(self, version: str, channel: str) -> ReleaseManifest:
        manifest = self.latest(channel)
        if str(manifest.version) != version:
            raise CwError(f"Local release {version} is unavailable", ErrorCode.UPDATE_CHECK_ERROR)
        return manifest


@dataclass(slots=True)
class LocalDownloader:
    """Test-only downloader whose roots must be declared explicitly."""

    allowed_root: Path

    def download(self, url: str, destination: Path) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise CwError("Local downloader only accepts file URLs", ErrorCode.UPDATE_DOWNLOAD_ERROR)
        source = Path(_local_file_path(parsed.path)).resolve()
        try:
            source.relative_to(self.allowed_root.resolve())
        except ValueError as exc:
            raise CwError("Local release path is outside its trusted root", ErrorCode.UPDATE_DOWNLOAD_ERROR) from exc
        destination.write_bytes(source.read_bytes())
