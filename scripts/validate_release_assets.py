#!/usr/bin/env python3
"""Validate the exact public asset allowlist for a Core-only release."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cw.update.models import ReleaseManifest


def expected_assets(version: str) -> set[str]:
    system = platform.system().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    return {
        f"codex_workflow-{version}-py3-none-any.whl",
        f"codex_workflow-{version}.tar.gz",
        f"cw-{version}-{system}-{machine}.tar.gz",
        "cw-release-manifest.json",
    }


def validate(directory: Path, version: str) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError("Core release directory is missing or unsafe")
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    if any(not item.is_file() or item.is_symlink() for item in entries):
        raise RuntimeError("Core release directory contains a non-regular entry")
    names = {item.name for item in entries}
    expected = expected_assets(version)
    if names != expected:
        raise RuntimeError(f"Core release asset inventory mismatch: expected {sorted(expected)}, observed {sorted(names)}")
    if any(name.startswith("cw-plugin-") or name.endswith(".zip") for name in names):
        raise RuntimeError("Plugin or ZIP assets are forbidden in a Core-only release")
    manifest = json.loads((directory / "cw-release-manifest.json").read_text(encoding="utf-8"))
    if "signature" in manifest:
        raise RuntimeError("Core-only manifest must not contain Plugin/signature extensions")
    parsed = ReleaseManifest.from_dict(manifest)
    if str(parsed.version) != version or parsed.channel != "stable" or parsed.plugin is not None:
        raise RuntimeError("Core-only manifest identity is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise RuntimeError("Core-only manifest must contain exactly one updater artifact")
    artifact = artifacts[0]
    archive = directory / str(artifact.get("filename", ""))
    if archive.name not in expected or not archive.name.startswith(f"cw-{version}-"):
        raise RuntimeError("Core manifest references an unexpected archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if artifact.get("sha256") != digest:
        raise RuntimeError("Core archive checksum does not match its manifest")
    return entries


def validate_existing_release(path: Path, assets: list[Path]) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise RuntimeError("Existing release asset metadata is invalid")
    observed = {str(item.get("name")): item for item in value if isinstance(item, dict)}
    if set(observed) != {item.name for item in assets}:
        raise RuntimeError("Existing GitHub Release asset inventory differs from the verified candidate")
    for asset in assets:
        remote = observed[asset.name]
        digest = str(remote.get("digest", "")).removeprefix("sha256:")
        if remote.get("size") != asset.stat().st_size or digest != hashlib.sha256(asset.read_bytes()).hexdigest():
            raise RuntimeError(f"Existing GitHub Release asset differs: {asset.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--component", choices=("core",), required=True)
    parser.add_argument("--existing-release-json", type=Path)
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version) is None:
        raise RuntimeError("Core VERSION is not a stable semantic version")
    assets = validate(args.directory, version)
    if args.existing_release_json:
        validate_existing_release(args.existing_release_json, assets)
    print(json.dumps({
        "component": "core",
        "version": version,
        "asset_count": len(assets),
        "assets": [item.name for item in assets],
        "plugin_included": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
