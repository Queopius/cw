from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cw.cli.commands.update import command_update
from cw.core.errors import CwError, ErrorCode
from cw.ui.console import Console
from cw.update.cache import UpdateCache
from cw.update.config import UpdateSettings, load_update_settings, set_update_setting
from cw.update.installation import InstallPaths, ManagedInstallation, safe_extract_release
from cw.update.models import ReleaseArtifact, ReleaseManifest, Version
from cw.update.provider import (
    HttpsDownloader,
    LocalDownloader,
    LocalReleaseProvider,
    _local_file_path,
    require_trusted_url,
)
from cw.update.service import UpdateService
from cw.update.service import automatic_update_notice


def machine() -> str:
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())


def make_release_tree(root: Path, version: str, *, smoke_ok: bool = True) -> Path:
    tree = root / f"tree-{version}"
    (tree / "cw").mkdir(parents=True)
    (tree / "cw/__init__.py").write_text("", encoding="utf-8")
    (tree / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tree / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    (tree / "NOTICE").write_text("Copyright 2026 Fantomid LLC\n", encoding="utf-8")
    if smoke_ok:
        script = f'import json\nprint(json.dumps({{"version": "{version}"}}))\n'
    else:
        script = 'raise SystemExit(7)\n'
    (tree / "entrypoint.py").write_text(script, encoding="utf-8")
    return tree


def archive_tree(root: Path, tree: Path, version: str) -> Path:
    archive = root / f"cw-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for path in sorted(tree.rglob("*")):
            stream.add(path, arcname=path.relative_to(tree), recursive=False)
    return archive


def manifest_dict(version: str, archive: Path, *, channel: str = "stable", checksum: str | None = None, arch: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "version": version,
        "channel": channel,
        "published_at": "2026-08-13T09:00:00Z",
        "minimum_project_schema": 1,
        "maximum_project_schema": 2,
        "artifacts": [{
            "platform": platform.system().lower(),
            "arch": arch or machine(),
            "url": archive.resolve().as_uri(),
            "sha256": checksum or hashlib.sha256(archive.read_bytes()).hexdigest(),
            "filename": archive.name,
        }],
        "release_notes": {
            "summary": "Verified local update fixture.",
            "url": "https://github.com/Queopius/cw/releases/tag/v" + version,
        },
    }


class CountingProvider(LocalReleaseProvider):
    def __init__(self, path: Path):
        super().__init__(path)
        self.calls = 0

    def latest(self, channel: str) -> ReleaseManifest:
        self.calls += 1
        return super().latest(channel)


class UpdateFixture:
    def __init__(
        self,
        base: Path,
        *,
        target: str = "0.2.0",
        current: str = "0.1.5",
        smoke_ok: bool = True,
    ):
        self.base = base
        self.paths = InstallPaths(base / "home/share/cw", base / "home/bin")
        old = make_release_tree(base, current)
        self.paths.versions.mkdir(parents=True)
        old_install = self.paths.versions / current
        old.rename(old_install)
        self.paths.share.mkdir(parents=True, exist_ok=True)
        module_path = old_install / "cw/update/installation.py"
        self.installation = ManagedInstallation(self.paths, module_path=module_path)
        self.installation.pointer.activate(current)
        self.archive = archive_tree(base, make_release_tree(base, target, smoke_ok=smoke_ok), target)
        self.manifest_path = base / "manifest.json"
        self.manifest_path.write_text(json.dumps(manifest_dict(target, self.archive)), encoding="utf-8")
        self.provider = CountingProvider(self.manifest_path)
        self.cache = UpdateCache(base / "config/update.json")
        self.service = UpdateService(
            self.provider, LocalDownloader(base), self.installation, self.cache,
            UpdateSettings(channel="stable", check=True, check_interval_hours=24),
        )


class UpdateModelTests(unittest.TestCase):
    def test_local_file_url_paths_are_native_and_percent_decoded(self):
        self.assertEqual(
            r"C:\Users\Ada Lovelace\release.tar.gz",
            _local_file_path("/C:/Users/Ada%20Lovelace/release.tar.gz", platform="nt"),
        )
        self.assertEqual(
            "/tmp/CW release.tar.gz",
            _local_file_path("/tmp/CW%20release.tar.gz", platform="posix"),
        )

    def test_semver_order_and_prerelease(self):
        self.assertLess(Version.parse("0.2.0-beta.1"), Version.parse("0.2.0"))
        self.assertLess(Version.parse("0.1.9"), Version.parse("0.2.0"))

    def test_invalid_manifest_fails_closed(self):
        with self.assertRaises(CwError) as caught:
            ReleaseManifest.from_dict({"schema_version": 99})
        self.assertEqual(ErrorCode.UPDATE_MANIFEST_ERROR, caught.exception.code)

    def test_duplicate_platform_artifact_fails(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); archive = root / "x"; archive.write_bytes(b"x")
            value = manifest_dict("0.2.0", archive)
            value["artifacts"].append(dict(value["artifacts"][0]))
            with self.assertRaises(CwError):
                ReleaseManifest.from_dict(value)

    def test_unexpected_platform_fails(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); archive = root / "x"; archive.write_bytes(b"x")
            value = manifest_dict("0.2.0", archive, arch="definitely-not-this-machine")
            with self.assertRaises(CwError) as caught:
                ReleaseManifest.from_dict(value).artifact_for_current_platform()
            self.assertEqual(ErrorCode.UPDATE_INCOMPATIBLE, caught.exception.code)

    def test_production_downloader_rejects_untrusted_url(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(CwError) as caught:
                HttpsDownloader().download("https://evil.example/cw.tar.gz", Path(name) / "x")
        self.assertEqual(ErrorCode.UPDATE_MANIFEST_ERROR, caught.exception.code)

    def test_github_release_asset_redirect_origin_is_trusted(self):
        require_trusted_url("https://release-assets.githubusercontent.com/github-production-release-asset/cw")

    def test_global_update_config_round_trip_on_python_310(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": name}, clear=False,
        ):
            set_update_setting("updates.channel", "beta")
            set_update_setting("updates.check", "false")
            settings = load_update_settings()
            self.assertEqual("beta", settings.channel)
            self.assertFalse(settings.check)
            self.assertIn("[updates]", (Path(name) / "cw/config.toml").read_text())


class UpdateServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-update-")
        self.fixture = UpdateFixture(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_update_available_and_no_automatic_install(self):
        info = self.fixture.service.check(force=True)
        self.assertTrue(info.available)
        self.assertEqual("0.1.5", self.fixture.installation.active_version())

    def test_no_update_available(self):
        fixture = UpdateFixture(Path(self.temporary.name) / "same", target="0.1.5")
        info = fixture.service.check(force=True)
        self.assertFalse(info.available)
        _, result = fixture.service.install()
        self.assertIsNone(result)

    def test_cached_check(self):
        self.fixture.service.check(force=True)
        self.fixture.service.check()
        self.assertEqual(1, self.fixture.provider.calls)

    def test_expired_cache(self):
        self.fixture.service.check(force=True)
        value = json.loads(self.fixture.cache.path.read_text())
        value["last_checked_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.fixture.cache.path.write_text(json.dumps(value), encoding="utf-8")
        self.fixture.service.check()
        self.assertEqual(2, self.fixture.provider.calls)

    def test_valid_staged_install_and_atomic_switch(self):
        _, result = self.fixture.service.install()
        self.assertIsNotNone(result)
        self.assertEqual("0.2.0", self.fixture.installation.active_version())
        self.assertEqual("0.2.0", self.fixture.installation.pointer.active_version())
        if os.name == "nt":
            self.assertEqual("0.2.0", self.fixture.paths.current.read_text(encoding="utf-8").strip())
        else:
            self.assertEqual(Path("versions/0.2.0"), self.fixture.paths.current.readlink())
        self.assertTrue((self.fixture.paths.versions / "0.1.5").is_dir())
        state = json.loads(self.fixture.paths.state.read_text())
        self.assertEqual("0.1.5", state["previous_version"])

    def test_0141_to_0151_rollback_and_reupdate_preserve_project_evidence(self):
        fixture = UpdateFixture(
            Path(self.temporary.name) / "core-0141-0151",
            current="0.14.1",
            target="0.15.1",
        )
        evidence = fixture.base / "consumer/.cw/state.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(b'{"legacy":true}\n')
        before = evidence.read_bytes()
        fixture.service.install()
        self.assertEqual("0.15.1", fixture.installation.active_version())
        self.assertEqual(before, evidence.read_bytes())
        self.assertEqual("0.14.1", fixture.service.rollback().current)
        self.assertEqual(before, evidence.read_bytes())
        fixture.service.install(requested_version="0.15.1")
        self.assertEqual("0.15.1", fixture.installation.active_version())
        self.assertEqual(before, evidence.read_bytes())

    def test_0150_to_0151_and_rollback_preserve_project_evidence(self):
        fixture = UpdateFixture(
            Path(self.temporary.name) / "core-0150-0151",
            current="0.15.0",
            target="0.15.1",
        )
        evidence = fixture.base / "consumer/.cw/state.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(b'{"legacy":true}\n')
        before = evidence.read_bytes()
        fixture.service.install()
        self.assertEqual("0.15.1", fixture.installation.active_version())
        self.assertEqual("0.15.0", fixture.service.rollback().current)
        self.assertEqual(before, evidence.read_bytes())

    def test_rollback_success(self):
        self.fixture.service.install()
        result = self.fixture.service.rollback()
        self.assertEqual("0.1.5", result.current)
        self.assertEqual("0.1.5", self.fixture.installation.active_version())

    def test_checksum_mismatch_preserves_current(self):
        value = json.loads(self.fixture.manifest_path.read_text())
        value["artifacts"][0]["sha256"] = "0" * 64
        self.fixture.manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(CwError) as caught:
            self.fixture.service.install()
        self.assertEqual(ErrorCode.UPDATE_CHECKSUM_ERROR, caught.exception.code)
        self.assertEqual("0.1.5", self.fixture.installation.active_version())

    def test_smoke_failure_preserves_current(self):
        fixture = UpdateFixture(Path(self.temporary.name) / "bad", smoke_ok=False)
        with self.assertRaises(CwError) as caught:
            fixture.service.install()
        self.assertEqual(ErrorCode.UPDATE_SMOKE_TEST_ERROR, caught.exception.code)
        self.assertEqual("0.1.5", fixture.installation.active_version())

    def test_stale_staging_cleanup(self):
        stale = self.fixture.paths.versions / ".staging-old"
        stale.mkdir()
        self.fixture.installation.cleanup_staging()
        self.assertFalse(stale.exists())

    def test_simultaneous_lock_fails(self):
        with self.fixture.installation.locked():
            with self.assertRaises(CwError) as caught:
                with self.fixture.installation.locked():
                    pass
        self.assertEqual(ErrorCode.LOCKED, caught.exception.code)

    def test_development_install_is_protected(self):
        development = ManagedInstallation(self.fixture.paths, module_path=self.fixture.base / "source/cw/update.py")
        service = UpdateService(
            self.fixture.provider, LocalDownloader(self.fixture.base), development,
            self.fixture.cache, self.fixture.service.settings,
        )
        with self.assertRaises(CwError) as caught:
            service.install()
        self.assertEqual(ErrorCode.UPDATE_DEVELOPMENT_INSTALL, caught.exception.code)

    def test_update_does_not_touch_project_data(self):
        project = self.fixture.base / "project/.cw/state.json"
        project.parent.mkdir(parents=True)
        project.write_text('{"sentinel": true}\n', encoding="utf-8")
        before = project.read_bytes()
        self.fixture.service.install()
        self.assertEqual(before, project.read_bytes())

    def test_update_cli_check_json(self):
        args = argparse.Namespace(
            rollback=False, check=True, info=False, version=None, channel=None,
            json=True, verbose=False, quiet=False, no_color=True,
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = command_update(args, Console(stream=output), service_factory=lambda: self.fixture.service)
        self.assertEqual(0, result)
        self.assertTrue(json.loads(output.getvalue())["available"])
        self.assertNotIn("\x1b", output.getvalue())

    def test_mock_cli_install_and_rollback_exercise_real_transaction(self):
        install_args = argparse.Namespace(
            rollback=False, check=False, info=False, version=None, channel=None,
            json=False, verbose=False, quiet=False, no_color=True,
        )
        output = io.StringIO()
        result = command_update(
            install_args, Console(stream=output, no_color=True),
            service_factory=lambda: self.fixture.service,
        )
        self.assertEqual(0, result)
        self.assertIn("SHA-256 verified", output.getvalue())
        self.assertEqual("0.2.0", self.fixture.installation.active_version())
        rollback_args = argparse.Namespace(**{**vars(install_args), "rollback": True})
        rollback_output = io.StringIO()
        result = command_update(
            rollback_args, Console(stream=rollback_output, no_color=True),
            service_factory=lambda: self.fixture.service,
        )
        self.assertEqual(0, result)
        self.assertIn("0.1.5 restored", rollback_output.getvalue())
        self.assertEqual("0.1.5", self.fixture.installation.active_version())

    def test_failed_background_check_is_non_blocking_and_cached(self):
        class BrokenProvider:
            calls = 0
            def latest(inner, _channel):
                inner.calls += 1
                raise CwError("offline", ErrorCode.UPDATE_CHECK_ERROR)
            def get(inner, _version, _channel):
                raise AssertionError
        provider = BrokenProvider()
        service = UpdateService(
            provider, self.fixture.service.downloader, self.fixture.installation,
            self.fixture.cache, self.fixture.service.settings,
        )
        with patch.dict(os.environ, {"CI": ""}):
            self.assertIsNone(service.cached_notice())
        self.assertTrue(self.fixture.cache.fresh("stable", 24))
        with patch.dict(os.environ, {"CI": ""}):
            self.assertIsNone(service.cached_notice())
        self.assertEqual(1, provider.calls)

    def test_ci_suppresses_automatic_notice(self):
        self.fixture.service.check(force=True)
        with patch.dict(os.environ, {"CI": "true"}):
            self.assertIsNone(self.fixture.service.cached_notice())

    def test_disabled_setting_suppresses_automatic_notice(self):
        self.fixture.service.settings = UpdateSettings(channel="stable", check=False, check_interval_hours=24)
        self.assertIsNone(self.fixture.service.cached_notice())
        self.assertEqual(0, self.fixture.provider.calls)

    def test_stable_channel_rejects_prerelease(self):
        value = json.loads(self.fixture.manifest_path.read_text())
        value["version"] = "0.2.0-beta.1"
        self.fixture.manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(CwError) as caught:
            self.fixture.service.check(force=True)
        self.assertEqual(ErrorCode.UPDATE_MANIFEST_ERROR, caught.exception.code)

    def test_rollback_smoke_failure_preserves_new_version(self):
        self.fixture.service.install()
        old_entrypoint = self.fixture.paths.versions / "0.1.5/entrypoint.py"
        old_entrypoint.write_text("raise SystemExit(9)\n", encoding="utf-8")
        with self.assertRaises(CwError) as caught:
            self.fixture.service.rollback()
        self.assertEqual(ErrorCode.UPDATE_SMOKE_TEST_ERROR, caught.exception.code)
        self.assertEqual("0.2.0", self.fixture.installation.active_version())

    def test_explicit_downgrade_required(self):
        manifest = ReleaseManifest.from_dict(json.loads(self.fixture.manifest_path.read_text()))
        old_artifact = manifest.artifacts[0]
        with patch.object(self.fixture.installation, "active_version", return_value="0.3.0"):
            with self.assertRaises(CwError) as caught:
                self.fixture.installation.install_release(manifest, old_artifact, self.fixture.archive)
        self.assertEqual(ErrorCode.UPDATE_INCOMPATIBLE, caught.exception.code)


class UpdateArchiveSecurityTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); archive = root / "bad.tar.gz"
            payload = root / "payload"; payload.write_text("bad", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as stream:
                stream.add(payload, arcname="../escape")
            destination = root / "stage"; destination.mkdir()
            with self.assertRaises(CwError):
                safe_extract_release(archive, destination)
            self.assertFalse((root / "escape").exists())

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); archive = root / "bad.tar.gz"
            info = tarfile.TarInfo("link"); info.type = tarfile.SYMTYPE; info.linkname = "/tmp"
            with tarfile.open(archive, "w:gz") as stream:
                stream.addfile(info)
            destination = root / "stage"; destination.mkdir()
            with self.assertRaises(CwError):
                safe_extract_release(archive, destination)


if __name__ == "__main__":
    unittest.main()
