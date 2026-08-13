#!/usr/bin/env python3
"""Build a local CW updater artifact and unsigned release manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import tarfile
import tempfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist/update"))
    parser.add_argument("--channel", choices=("stable", "beta", "dev"), default="stable")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"cw-{version}-{platform.system().lower()}-{platform.machine().lower()}.tar.gz"
    with tempfile.TemporaryDirectory() as name:
        stage = Path(name)
        shutil.copytree(root / "cw", stage / "cw", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for item in ("VERSION", "LICENSE", "NOTICE", "CHANGELOG.md"):
            shutil.copy2(root / item, stage / item)
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            capture_output=True, timeout=5, check=False,
        )
        commit = completed.stdout.strip() if completed.returncode == 0 else "unknown"
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
            text=True, capture_output=True, timeout=5, check=False,
        )
        if commit != "unknown" and dirty.returncode == 0 and dirty.stdout.strip():
            commit += "-dirty"
        (stage / "BUILD.json").write_text(
            json.dumps({"schema_version": 1, "commit": commit, "source": "release-artifact"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (stage / "entrypoint.py").write_text(
            "#!/usr/bin/env python3\nfrom cw.cli.main import main\nraise SystemExit(main())\n", encoding="utf-8",
        )
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(stage.rglob("*")):
                tar.add(path, arcname=path.relative_to(stage), recursive=False)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    machine = {"AMD64": "x86_64", "aarch64": "arm64"}.get(platform.machine(), platform.machine()).lower()
    manifest = {
        "schema_version": 1, "version": version, "channel": args.channel,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "minimum_project_schema": 1, "maximum_project_schema": 1,
        "artifacts": [{
            "platform": platform.system().lower(), "arch": machine,
            "url": f"https://github.com/Queopius/cw/releases/download/v{version}/{archive.name}",
            "sha256": digest, "filename": archive.name,
        }],
        "release_notes": {
            "summary": "See the bundled CW changelog for verified release details.",
            "url": f"https://github.com/Queopius/cw/releases/tag/v{version}",
        },
    }
    (args.output / "cw-release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
