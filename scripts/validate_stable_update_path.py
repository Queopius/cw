#!/usr/bin/env python3
"""Exercise a published stable Core update/rollback against a candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


STABLE_PROGRAM = r'''
import hashlib
import json
import os
from pathlib import Path

from cw import __version__
from cw.update.cache import UpdateCache
from cw.update.config import UpdateSettings
from cw.update.installation import InstallPaths, ManagedInstallation, safe_extract_release
from cw.update.models import ReleaseManifest
from cw.update.provider import LocalDownloader, LocalReleaseProvider
from cw.update.service import UpdateService

root = Path(os.environ["CW_COMPAT_ROOT"])
manifest_path = Path(os.environ["CW_CANDIDATE_MANIFEST"])
archive = Path(os.environ["CW_CANDIDATE_ARCHIVE"])
stable_archive = Path(os.environ["CW_STABLE_ARCHIVE"])
candidate_commit = os.environ["CW_CANDIDATE_COMMIT"]
candidate_version = os.environ["CW_CANDIDATE_VERSION"]
stable_version = os.environ["CW_STABLE_VERSION"]
assert __version__ == stable_version, __version__
document = json.loads(manifest_path.read_text(encoding="utf-8"))
assert "signature" not in document
artifact = document["artifacts"][0]
artifact["url"] = archive.as_uri()
local_manifest = root / "candidate-manifest.json"
local_manifest.write_text(json.dumps(document), encoding="utf-8")
parsed = ReleaseManifest.from_dict(document)
assert str(parsed.version) == candidate_version
assert artifact["filename"] == archive.name
assert artifact["filename"].startswith("cw-" + candidate_version + "-")

paths = InstallPaths(root / "managed", root / "bin")
stable = paths.versions / stable_version
stable.mkdir(parents=True)
safe_extract_release(stable_archive, stable)
paths.share.mkdir(parents=True, exist_ok=True)
paths.current.symlink_to(Path("versions") / stable_version, target_is_directory=True)
installation = ManagedInstallation(paths, module_path=stable / "cw" / "update" / "installation.py")
service = UpdateService(
    LocalReleaseProvider(local_manifest), LocalDownloader(archive.parent), installation,
    UpdateCache(root / "update-cache.json"), UpdateSettings(channel="stable", check=True),
)

info, installed = service.install()
assert str(info.installed) == stable_version and str(info.latest) == candidate_version
assert installed is not None and installed.current == candidate_version
candidate = paths.versions / candidate_version
build = json.loads((candidate / "BUILD.json").read_text(encoding="utf-8"))
assert build == {"schema_version": 1, "commit": candidate_commit, "source": "release-artifact"}
rolled_back = service.rollback()
assert rolled_back.current == stable_version
_, reinstalled = service.install(requested_version=candidate_version)
assert reinstalled is not None and reinstalled.current == candidate_version
sentinel = root / "consumer" / "sentinel.txt"
assert sentinel.read_text(encoding="utf-8") == "preserve\n"
assert not (root / "consumer" / ".cw").exists()
print(json.dumps({
    "stable_consumer": __version__, "core_only_manifest": True,
    "update": stable_version + " -> " + candidate_version,
    "rollback": candidate_version + " -> " + stable_version,
    "reupdate": stable_version + " -> " + candidate_version,
    "build_commit": build["commit"],
    "projects_preserved": True,
}, sort_keys=True))
'''


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_version(
    manifest_path: Path, archive: Path, version_path: Path, stable_version: str
) -> str:
    """Bind the archive, manifest, and source VERSION before invoking stable CW."""

    from cw.update.models import ReleaseManifest, Version

    if not version_path.is_file() or version_path.is_symlink():
        raise RuntimeError("candidate VERSION file is missing or unsafe")
    source_version = version_path.read_text(encoding="utf-8").strip()
    parsed_source_version = Version.parse(source_version)
    if not Version.parse(stable_version) < parsed_source_version:
        raise RuntimeError("candidate version must be newer than the stable version")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError("candidate manifest is missing or unsafe")
    if not archive.is_file() or archive.is_symlink():
        raise RuntimeError("candidate archive is missing or unsafe")
    try:
        manifest = ReleaseManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("candidate manifest is invalid") from exc
    candidate_version = str(manifest.version)
    if candidate_version != source_version:
        raise RuntimeError("candidate VERSION and manifest version differ")
    artifact = manifest.artifact_for_current_platform()
    if artifact.filename != archive.name or not archive.name.startswith(f"cw-{candidate_version}-"):
        raise RuntimeError("candidate archive identity does not match manifest version")
    if _digest(archive) != artifact.sha256:
        raise RuntimeError("candidate archive checksum does not match manifest")
    if manifest.plugin is not None:
        raise RuntimeError("candidate Core manifest unexpectedly includes Plugin metadata")
    return candidate_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stable-wheel", type=Path, required=True)
    parser.add_argument("--stable-version", required=True)
    parser.add_argument("--stable-wheel-sha256", required=True)
    parser.add_argument("--stable-archive", type=Path, required=True)
    parser.add_argument("--stable-archive-sha256", required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--candidate-archive", type=Path, required=True)
    parser.add_argument("--candidate-version-file", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    args = parser.parse_args()
    from cw.update.models import Version

    Version.parse(args.stable_version)
    if _digest(args.stable_wheel) != args.stable_wheel_sha256:
        raise RuntimeError("published stable wheel checksum mismatch")
    if _digest(args.stable_archive) != args.stable_archive_sha256:
        raise RuntimeError("published stable Core archive checksum mismatch")
    if not args.candidate_commit or len(args.candidate_commit) != 40:
        raise RuntimeError("candidate commit must be an exact SHA")
    candidate_version = _candidate_version(
        args.candidate_manifest, args.candidate_archive, args.candidate_version_file,
        args.stable_version,
    )
    with tempfile.TemporaryDirectory(prefix="cw-stable-updater-") as name:
        root = Path(name)
        consumer = root / "consumer"
        consumer.mkdir()
        (consumer / "sentinel.txt").write_text("preserve\n", encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "PYTHONPATH": str(args.stable_wheel.resolve()),
            "CW_COMPAT_ROOT": str(root),
            "CW_CANDIDATE_MANIFEST": str(args.candidate_manifest.resolve()),
            "CW_CANDIDATE_ARCHIVE": str(args.candidate_archive.resolve()),
            "CW_CANDIDATE_VERSION": candidate_version,
            "CW_STABLE_VERSION": args.stable_version,
            "CW_STABLE_ARCHIVE": str(args.stable_archive.resolve()),
            "CW_CANDIDATE_COMMIT": args.candidate_commit,
            "CW_NO_UPDATE_CHECK": "1",
        })
        result = subprocess.run(
            [sys.executable, "-c", STABLE_PROGRAM], cwd=root, env=environment,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=120, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"stable updater compatibility failed:\n{result.stdout}\n{result.stderr}")
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
