from __future__ import annotations

import gzip
import hashlib
import json
import platform
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from cw.update.models import ReleaseManifest
from scripts.build_release import _normalized_tar
from scripts.validate_release_assets import (
    expected_assets,
    validate as validate_assets,
    validate_existing_release,
)
from scripts.validate_release_provenance import ZERO_SHA, validate as validate_provenance


ROOT = Path(__file__).resolve().parents[1]


def run(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, text=True,
        encoding="utf-8", capture_output=True,
    ).stdout.strip()


class CoreReleaseManifestTests(unittest.TestCase):
    def test_core_only_manifest_is_valid_without_plugin(self) -> None:
        manifest = {
            "schema_version": 1,
            "version": "0.15.0",
            "channel": "stable",
            "published_at": "2026-08-21T00:00:00Z",
            "minimum_project_schema": 1,
            "maximum_project_schema": 1,
            "artifacts": [{
                "platform": platform.system().lower(),
                "arch": platform.machine().lower(),
                "url": "https://github.com/Queopius/cw/releases/download/v0.15.0/cw-0.15.0-test.tar.gz",
                "sha256": "a" * 64,
                "filename": "cw-0.15.0-test.tar.gz",
            }],
            "release_notes": {
                "summary": "Core-only",
                "url": "https://github.com/Queopius/cw/releases/tag/v0.15.0",
            },
        }
        parsed = ReleaseManifest.from_dict(manifest)
        self.assertIsNone(parsed.plugin)
        self.assertIsNone(parsed.signature)

    def test_legacy_plugin_extension_remains_accepted(self) -> None:
        manifest = {
            "schema_version": 1,
            "version": "0.14.1",
            "channel": "stable",
            "published_at": "2026-08-20T22:57:37Z",
            "minimum_project_schema": 1,
            "maximum_project_schema": 1,
            "artifacts": [{
                "platform": "linux", "arch": "x86_64",
                "url": "https://github.com/Queopius/cw/releases/download/v0.14.1/cw-0.14.1-linux-x86_64.tar.gz",
                "sha256": "a" * 64, "filename": "cw-0.14.1-linux-x86_64.tar.gz",
            }],
            "release_notes": {"summary": "Legacy", "url": "https://github.com/Queopius/cw/releases/tag/v0.14.1"},
            "signature": {"extensions": {"plugin": {
                "name": "cw", "version": "0.1.0",
                "asset": {"filename": "cw-plugin-0.1.0.zip", "sha256": "b" * 64, "size": 546684},
                "core_compatibility": {"minimum": "0.14.0", "maximum_exclusive": "1.0.0"},
                "provenance": {"source_commit": "9eb289cb", "builder": "scripts/build_plugin_candidate.py"},
            }}},
        }
        self.assertEqual("0.1.0", str(ReleaseManifest.from_dict(manifest).plugin.version))

    def test_exact_asset_allowlist_rejects_plugin_and_unexpected_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            version = "0.15.0"
            names = expected_assets(version)
            archive_name = next(
                item for item in names if item.startswith("cw-") and item.endswith(".tar.gz")
            )
            archive = directory / archive_name
            archive.write_bytes(b"core")
            for filename in names - {archive_name, "cw-release-manifest.json"}:
                (directory / filename).write_bytes(b"distribution")
            manifest = {
                "schema_version": 1, "version": version, "channel": "stable",
                "published_at": "2026-08-21T00:00:00Z",
                "minimum_project_schema": 1, "maximum_project_schema": 1,
                "artifacts": [{
                    "platform": platform.system().lower(), "arch": platform.machine().lower(),
                    "url": f"https://github.com/Queopius/cw/releases/download/v{version}/{archive.name}",
                    "filename": archive.name,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }],
                "release_notes": {
                    "summary": "Core-only", "url": f"https://github.com/Queopius/cw/releases/tag/v{version}",
                },
            }
            (directory / "cw-release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(4, len(validate_assets(directory, version)))
            plugin = directory / "cw-plugin-0.1.0.zip"
            plugin.write_bytes(b"forbidden")
            with self.assertRaisesRegex(RuntimeError, "inventory mismatch"):
                validate_assets(directory, version)
            plugin.unlink()
            unexpected = directory / "notes.txt"
            unexpected.write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "inventory mismatch"):
                validate_assets(directory, version)

    def test_existing_release_must_match_every_verified_asset(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            assets = []
            metadata = []
            for filename in sorted(expected_assets("0.15.0")):
                path = root / filename
                path.write_bytes(filename.encode("utf-8"))
                assets.append(path)
                metadata.append({
                    "name": filename, "size": path.stat().st_size,
                    "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
                })
            existing = root / "existing.json"
            existing.write_text(json.dumps(metadata), encoding="utf-8")
            validate_existing_release(existing, assets)
            metadata[0]["digest"] = "sha256:" + "0" * 64
            existing.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "differs"):
                validate_existing_release(existing, assets)

    def test_normalized_core_archive_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            stage = root / "stage"
            stage.mkdir()
            (stage / "entrypoint.py").write_text("print('ok')\n", encoding="utf-8")
            (stage / "VERSION").write_text("0.15.0\n", encoding="utf-8")
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            _normalized_tar(stage, first)
            _normalized_tar(stage, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with gzip.open(first, "rb") as stream:
                with tarfile.open(fileobj=stream, mode="r:") as archive:
                    self.assertTrue(all(item.mtime == 0 for item in archive.getmembers()))


class ReleaseProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-release-provenance-")
        self.root = Path(self.temporary.name)
        run(self.root, "init", "-q")
        run(self.root, "config", "user.name", "CW Test")
        run(self.root, "config", "user.email", "cw@example.invalid")
        (self.root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        run(self.root, "add", "VERSION")
        run(self.root, "commit", "-qm", "prod")
        self.prod = run(self.root, "rev-parse", "HEAD")
        run(self.root, "branch", "prod")
        run(self.root, "tag", "-a", "v0.15.0", "-m", "CW 0.15.0", self.prod)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_annotated_tag_contained_in_prod_passes(self) -> None:
        validate_provenance(self.root, "v0.15.0", self.prod, "prod", ZERO_SHA)

    def test_non_prod_branch_tags_fail_closed(self) -> None:
        (self.root / "candidate").write_text("candidate", encoding="utf-8")
        run(self.root, "add", "candidate")
        run(self.root, "commit", "-qm", "candidate")
        candidate = run(self.root, "rev-parse", "HEAD")
        run(self.root, "tag", "-d", "v0.15.0")
        for branch in ("dev", "staging", "release"):
            run(self.root, "branch", branch, candidate)
            if run(self.root, "tag", "-l", "v0.15.0"):
                run(self.root, "tag", "-d", "v0.15.0")
            run(self.root, "tag", "-a", "v0.15.0", "-m", branch, candidate)
            with self.subTest(branch=branch), self.assertRaises(RuntimeError):
                validate_provenance(self.root, "v0.15.0", candidate, "prod", ZERO_SHA)

    def test_lightweight_moved_and_version_mismatch_fail_closed(self) -> None:
        run(self.root, "tag", "-d", "v0.15.0")
        run(self.root, "tag", "v0.15.0", self.prod)
        with self.assertRaises(RuntimeError):
            validate_provenance(self.root, "v0.15.0", self.prod, "prod", ZERO_SHA)
        with self.assertRaisesRegex(RuntimeError, "moved"):
            validate_provenance(self.root, "v0.15.0", self.prod, "prod", "1" * 40)
        (self.root / "VERSION").write_text("0.15.1\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "VERSION"):
            validate_provenance(self.root, "v0.15.0", self.prod, "prod", ZERO_SHA)


if __name__ == "__main__":
    unittest.main()
