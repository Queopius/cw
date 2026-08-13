from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from datetime import datetime
from functools import total_ordering
from typing import Any

from cw.core.errors import CwError, ErrorCode


_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
CHANNELS = {"stable", "beta", "dev"}


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = _VERSION.fullmatch(value.strip())
        if not match:
            raise CwError(
                f"Invalid release version: {value}",
                ErrorCode.UPDATE_MANIFEST_ERROR,
            )
        return cls(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            tuple((match.group(4) or "").split(".")) if match.group(4) else (),
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.prerelease)}" if self.prerelease else base

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        return _prerelease_key(self.prerelease) < _prerelease_key(other.prerelease)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    platform: str
    arch: str
    url: str
    sha256: str
    filename: str

    @classmethod
    def from_dict(cls, value: Any) -> "ReleaseArtifact":
        if not isinstance(value, dict):
            raise _manifest_error("Release artifact must be an object")
        required = {"platform", "arch", "url", "sha256", "filename"}
        if set(value) != required or not all(isinstance(value[key], str) and value[key] for key in required):
            raise _manifest_error("Release artifact fields are invalid")
        checksum = value["sha256"].removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise _manifest_error("Release artifact SHA-256 is invalid")
        filename = value["filename"]
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise _manifest_error("Release artifact filename is unsafe")
        return cls(
            platform=value["platform"], arch=value["arch"], url=value["url"],
            sha256=checksum, filename=filename,
        )


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    schema_version: int
    version: Version
    channel: str
    published_at: str
    minimum_project_schema: int
    maximum_project_schema: int
    artifacts: tuple[ReleaseArtifact, ...]
    summary: str
    release_url: str
    signature: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "ReleaseManifest":
        if not isinstance(value, dict):
            raise _manifest_error("Release manifest must be an object")
        allowed = {
            "schema_version", "version", "channel", "published_at",
            "minimum_project_schema", "maximum_project_schema", "artifacts",
            "release_notes", "signature",
        }
        if set(value) - allowed:
            raise _manifest_error("Release manifest contains unknown fields")
        if value.get("schema_version") != 1:
            raise _manifest_error("Unsupported release manifest schema")
        channel = value.get("channel")
        if channel not in CHANNELS:
            raise _manifest_error("Release channel is invalid")
        published = value.get("published_at")
        if not isinstance(published, str):
            raise _manifest_error("Release timestamp is missing")
        try:
            parsed_published = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _manifest_error("Release timestamp is invalid") from exc
        if parsed_published.tzinfo is None:
            raise _manifest_error("Release timestamp must include a timezone")
        minimum = value.get("minimum_project_schema")
        maximum = value.get("maximum_project_schema")
        if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum < 1 or maximum < minimum:
            raise _manifest_error("Project schema compatibility range is invalid")
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise _manifest_error("Release manifest has no artifacts")
        parsed = tuple(ReleaseArtifact.from_dict(item) for item in artifacts)
        identities = [(item.platform, item.arch) for item in parsed]
        if len(identities) != len(set(identities)):
            raise _manifest_error("Release manifest contains duplicate platform artifacts")
        notes = value.get("release_notes")
        if not isinstance(notes, dict) or set(notes) != {"summary", "url"}:
            raise _manifest_error("Release notes metadata is invalid")
        if not all(isinstance(notes[key], str) for key in notes):
            raise _manifest_error("Release notes metadata is invalid")
        signature = value.get("signature")
        if signature is not None and not isinstance(signature, dict):
            raise _manifest_error("Release signature metadata is invalid")
        return cls(
            schema_version=1, version=Version.parse(str(value.get("version", ""))),
            channel=channel, published_at=published,
            minimum_project_schema=minimum, maximum_project_schema=maximum,
            artifacts=parsed, summary=notes["summary"], release_url=notes["url"],
            signature=signature,
        )

    def artifact_for_current_platform(self) -> ReleaseArtifact:
        system = platform.system().lower()
        machine = platform.machine().lower()
        aliases = {"amd64": "x86_64", "aarch64": "arm64"}
        machine = aliases.get(machine, machine)
        for artifact in self.artifacts:
            if artifact.platform == system and aliases.get(artifact.arch, artifact.arch) == machine:
                return artifact
        raise CwError(
            f"No CW release is available for {system}/{machine}",
            ErrorCode.UPDATE_INCOMPATIBLE,
        )


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    installed: Version
    latest: Version
    channel: str
    manifest: ReleaseManifest

    @property
    def available(self) -> bool:
        return self.latest > self.installed

    @property
    def level(self) -> str:
        if self.latest.major != self.installed.major:
            return "major"
        if self.latest.minor != self.installed.minor:
            return "minor"
        return "patch"


def _manifest_error(message: str) -> CwError:
    return CwError(message, ErrorCode.UPDATE_MANIFEST_ERROR, "Run: cw error")


def _prerelease_key(values: tuple[str, ...]) -> tuple[tuple[int, int | str], ...]:
    return tuple((0, int(value)) if value.isdigit() else (1, value) for value in values)
