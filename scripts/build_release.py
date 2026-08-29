#!/usr/bin/env python3
"""Build one deterministic Core updater archive and a Core-only manifest."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cw.update.models import ReleaseManifest


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=10, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _source_identity() -> tuple[str, str]:
    commit = _git("rev-parse", "HEAD")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError("release source commit is invalid")
    if _git("status", "--porcelain", "--untracked-files=normal"):
        raise RuntimeError("Core release candidates must be built from a clean worktree")
    timestamp = datetime.fromisoformat(_git("show", "-s", "--format=%cI", "HEAD"))
    return commit, timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalized_tar(stage: Path, destination: Path) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(stage.rglob("*"), key=lambda item: item.relative_to(stage).as_posix()):
                    if path.is_symlink():
                        raise RuntimeError(f"release stage contains a symlink: {path.relative_to(stage)}")
                    relative = path.relative_to(stage).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    info.mode = 0o755 if path.is_dir() or relative == "entrypoint.py" else 0o644
                    if path.is_file():
                        with path.open("rb") as source:
                            archive.addfile(info, source)
                    else:
                        archive.addfile(info)


def build_core(output: Path, channel: str) -> dict[str, object]:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    commit, published_at = _source_identity()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name.startswith("cw-plugin-") for path in output.iterdir()):
        raise RuntimeError("Plugin candidates are forbidden in the Core release directory")
    system = platform.system().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    archive_path = output / f"cw-{version}-{system}-{machine}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="cw-core-release-") as name:
        stage = Path(name)
        shutil.copytree(ROOT / "cw", stage / "cw", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for item in ("VERSION", "LICENSE", "NOTICE", "CHANGELOG.md", "pyproject.toml"):
            shutil.copy2(ROOT / item, stage / item)
        (stage / "BUILD.json").write_text(
            json.dumps({"schema_version": 1, "commit": commit, "source": "release-artifact"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (stage / "entrypoint.py").write_text(
            "#!/usr/bin/env python3\nfrom cw.cli.main import main\nraise SystemExit(main())\n", encoding="utf-8",
        )
        _normalized_tar(stage, archive_path)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "version": version,
        "channel": channel,
        "published_at": published_at,
        "minimum_project_schema": 1,
        "maximum_project_schema": 1,
        "artifacts": [{
            "platform": system,
            "arch": machine,
            "url": f"https://github.com/Queopius/cw/releases/download/v{version}/{archive_path.name}",
            "sha256": digest,
            "filename": archive_path.name,
        }],
        "release_notes": {
            "summary": "See the bundled CW changelog for verified release details.",
            "url": f"https://github.com/Queopius/cw/releases/tag/v{version}",
        },
    }
    parsed = ReleaseManifest.from_dict(manifest)
    if parsed.plugin is not None or "signature" in manifest:
        raise RuntimeError("Core-only release manifest unexpectedly contains Plugin metadata")
    manifest_path = output / "cw-release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "component": "core",
        "version": version,
        "commit": commit,
        "archive": str(archive_path),
        "archive_sha256": digest,
        "manifest": str(manifest_path),
        "plugin_included": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--channel", choices=("stable", "beta", "dev"), default="stable")
    parser.add_argument("--component", choices=("core",), required=True)
    args = parser.parse_args()
    print(json.dumps(build_core(args.output, args.channel), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
