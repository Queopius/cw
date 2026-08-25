#!/usr/bin/env python3
"""Exercise Core-only update/rollback using the published 0.14.1 runtime."""
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
assert __version__ == "0.14.1", __version__
document = json.loads(manifest_path.read_text(encoding="utf-8"))
assert "signature" not in document
artifact = document["artifacts"][0]
artifact["url"] = archive.as_uri()
local_manifest = root / "candidate-manifest.json"
local_manifest.write_text(json.dumps(document), encoding="utf-8")
parsed = ReleaseManifest.from_dict(document)
assert str(parsed.version) == "0.17.0"

paths = InstallPaths(root / "managed", root / "bin")
stable = paths.versions / "0.14.1"
stable.mkdir(parents=True)
safe_extract_release(stable_archive, stable)
paths.share.mkdir(parents=True, exist_ok=True)
paths.current.symlink_to(Path("versions") / "0.14.1", target_is_directory=True)
installation = ManagedInstallation(paths, module_path=stable / "cw" / "update" / "installation.py")
service = UpdateService(
    LocalReleaseProvider(local_manifest), LocalDownloader(archive.parent), installation,
    UpdateCache(root / "update-cache.json"), UpdateSettings(channel="stable", check=True),
)

info, installed = service.install()
assert str(info.installed) == "0.14.1" and str(info.latest) == "0.17.0"
assert installed is not None and installed.current == "0.17.0"
candidate = paths.versions / "0.17.0"
build = json.loads((candidate / "BUILD.json").read_text(encoding="utf-8"))
assert build == {"schema_version": 1, "commit": candidate_commit, "source": "release-artifact"}
rolled_back = service.rollback()
assert rolled_back.current == "0.14.1"
_, reinstalled = service.install(requested_version="0.17.0")
assert reinstalled is not None and reinstalled.current == "0.17.0"
sentinel = root / "consumer" / "sentinel.txt"
assert sentinel.read_text(encoding="utf-8") == "preserve\n"
assert not (root / "consumer" / ".cw").exists()
print(json.dumps({
    "stable_consumer": __version__, "core_only_manifest": True,
    "update": "0.14.1 -> 0.17.0", "rollback": "0.17.0 -> 0.14.1",
    "reupdate": "0.14.1 -> 0.17.0", "build_commit": build["commit"],
    "projects_preserved": True,
}, sort_keys=True))
'''


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stable-wheel", type=Path, required=True)
    parser.add_argument("--stable-wheel-sha256", required=True)
    parser.add_argument("--stable-archive", type=Path, required=True)
    parser.add_argument("--stable-archive-sha256", required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--candidate-archive", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    args = parser.parse_args()
    if _digest(args.stable_wheel) != args.stable_wheel_sha256:
        raise RuntimeError("published 0.14.1 wheel checksum mismatch")
    if _digest(args.stable_archive) != args.stable_archive_sha256:
        raise RuntimeError("published 0.14.1 Core archive checksum mismatch")
    if not args.candidate_commit or len(args.candidate_commit) != 40:
        raise RuntimeError("candidate commit must be an exact SHA")
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
