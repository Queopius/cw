from __future__ import annotations

import hashlib
import json
import platform
import tempfile
import unittest
from pathlib import Path

from scripts.validate_stable_update_path import _candidate_version


def _platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return system, machine


class StableUpdateCandidateIdentityTests(unittest.TestCase):
    def _write_candidate(
        self, root: Path, *, version: str = "0.18.0", manifest_version: str | None = None,
        archive_name: str | None = None, plugin: bool = False,
    ) -> tuple[Path, Path, Path]:
        system, machine = _platform()
        candidate_version = manifest_version or version
        archive = root / (archive_name or f"cw-{candidate_version}-{system}-{machine}.tar.gz")
        archive.write_bytes(b"deterministic candidate archive\n")
        artifact = {
            "platform": system,
            "arch": machine,
            "url": archive.as_uri(),
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "filename": archive.name,
        }
        manifest: dict[str, object] = {
            "schema_version": 1,
            "version": candidate_version,
            "channel": "stable",
            "published_at": "2026-08-26T00:00:00Z",
            "minimum_project_schema": 1,
            "maximum_project_schema": 1,
            "artifacts": [artifact],
            "release_notes": {"summary": "candidate", "url": "https://example.invalid/release"},
        }
        if plugin:
            manifest["signature"] = {"extensions": {"plugin": {
                "name": "cw", "version": "0.1.0",
                "asset": {"filename": "cw-plugin-0.1.0.zip", "sha256": "0" * 64, "size": 1},
                "core_compatibility": {"minimum": "0.1.0", "maximum_exclusive": "1.0.0"},
                "provenance": {"source_commit": "1" * 40, "builder": "scripts/build_plugin_candidate.py"},
            }}}
        manifest_path = root / "cw-release-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        version_path = root / "VERSION"
        version_path.write_text(version + "\n", encoding="utf-8")
        return manifest_path, archive, version_path

    def test_stable_017_to_candidate_018_is_bound_to_manifest_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            manifest, archive, version = self._write_candidate(Path(name))
            self.assertEqual("0.18.0", _candidate_version(manifest, archive, version, "0.17.0"))

    def test_wrong_archive_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            manifest, archive, version = self._write_candidate(
                Path(name), archive_name="cw-0.17.0-linux-x86_64.tar.gz"
            )
            with self.assertRaisesRegex(RuntimeError, "archive identity"):
                _candidate_version(manifest, archive, version, "0.17.0")

    def test_version_and_manifest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            manifest, archive, version = self._write_candidate(
                Path(name), version="0.18.0", manifest_version="0.18.1"
            )
            with self.assertRaisesRegex(RuntimeError, "VERSION and manifest"):
                _candidate_version(manifest, archive, version, "0.17.0")

    def test_missing_candidate_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            manifest, archive, version = self._write_candidate(Path(name))
            archive.unlink()
            with self.assertRaisesRegex(RuntimeError, "archive is missing"):
                _candidate_version(manifest, archive, version, "0.17.0")

    def test_plugin_metadata_is_rejected_from_core_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            manifest, archive, version = self._write_candidate(Path(name), plugin=True)
            with self.assertRaisesRegex(RuntimeError, "Plugin metadata"):
                _candidate_version(manifest, archive, version, "0.17.0")


if __name__ == "__main__":
    unittest.main()
