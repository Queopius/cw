from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.build_plugin_candidate import FIXED_TIME, build
from scripts.prepare_plugin_marketplace import (
    DistributionError,
    MARKETPLACE_RELATIVE,
    PLUGIN_RELATIVE,
    prepare_marketplace,
    validate_marketplace_root,
)
from scripts.validate_plugin_candidate import ROOT


def _copy_marketplace_fixture(destination: Path) -> Path:
    (destination / MARKETPLACE_RELATIVE.parent).mkdir(parents=True)
    shutil.copy2(ROOT / MARKETPLACE_RELATIVE, destination / MARKETPLACE_RELATIVE)
    shutil.copytree(ROOT / PLUGIN_RELATIVE, destination / PLUGIN_RELATIVE)
    return destination


class MarketplaceContractTests(unittest.TestCase):
    def test_repository_marketplace_resolves_from_arbitrary_root(self) -> None:
        self.assertEqual((ROOT / PLUGIN_RELATIVE).resolve(), validate_marketplace_root(ROOT))
        with tempfile.TemporaryDirectory(prefix="cw-marketplace-arbitrary-") as name:
            arbitrary = _copy_marketplace_fixture(Path(name) / "unrelated-name")
            self.assertEqual((arbitrary / PLUGIN_RELATIVE).resolve(), validate_marketplace_root(arbitrary))

    def test_marketplace_paths_and_schema_fail_closed(self) -> None:
        invalid_paths = (
            "plugins/cw", "../plugins/cw", "./../cw", "/plugins/cw",
            "./C:/plugins/cw", ".\\plugins\\cw", "./plugins/cw/../other",
        )
        with tempfile.TemporaryDirectory(prefix="cw-marketplace-invalid-") as name:
            base = Path(name)
            for index, invalid in enumerate(invalid_paths):
                root = _copy_marketplace_fixture(base / str(index))
                payload = json.loads((root / MARKETPLACE_RELATIVE).read_text(encoding="utf-8"))
                payload["plugins"][0]["source"]["path"] = invalid
                (root / MARKETPLACE_RELATIVE).write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(path=invalid), self.assertRaises(DistributionError):
                    validate_marketplace_root(root)

            root = _copy_marketplace_fixture(base / "unknown")
            payload = json.loads((root / MARKETPLACE_RELATIVE).read_text(encoding="utf-8"))
            payload["plugins"][0]["unexpected"] = True
            (root / MARKETPLACE_RELATIVE).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(DistributionError):
                validate_marketplace_root(root)

    def test_marketplace_rejects_symlinks_and_incomplete_plugins(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-marketplace-symlink-") as name:
            base = Path(name)
            root = _copy_marketplace_fixture(base / "root")
            manifest = root / PLUGIN_RELATIVE / ".codex-plugin/plugin.json"
            manifest.unlink()
            manifest.symlink_to(ROOT / PLUGIN_RELATIVE / ".codex-plugin/plugin.json")
            with self.assertRaises(DistributionError):
                validate_marketplace_root(root)

            incomplete = _copy_marketplace_fixture(base / "incomplete")
            (incomplete / PLUGIN_RELATIVE / ".mcp.json").unlink()
            with self.assertRaises(DistributionError):
                validate_marketplace_root(incomplete)

            manipulated = _copy_marketplace_fixture(base / "manipulated")
            (manipulated / PLUGIN_RELATIVE / ".mcp.json").write_text(json.dumps({
                "mcpServers": {"cw": {"command": "sh", "args": ["-c", "id"]}},
            }), encoding="utf-8")
            with self.assertRaises(DistributionError):
                validate_marketplace_root(manipulated)


class PluginRuntimeFailureTests(unittest.TestCase):
    def test_uninitialized_repository_fails_without_creating_state_or_modifying_git(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-uninitialized-") as name:
            repository = Path(name) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            before = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repository,
                capture_output=True, text=True, encoding="utf-8", check=True,
            ).stdout
            environment = {**os.environ, "PYTHONPATH": str(ROOT)}
            completed = subprocess.run(
                [
                    "python3", "-m", "cw", "mcp", "serve",
                    "--allowed-root", str(repository), "--project", str(repository),
                ],
                cwd=repository, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", check=False, timeout=15,
            )
            self.assertEqual(1, completed.returncode)
            self.assertIn("PROJECT_NOT_INITIALIZED", completed.stderr)
            self.assertFalse((repository / ".cw").exists())
            after = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repository,
                capture_output=True, text=True, encoding="utf-8", check=True,
            ).stdout
            self.assertEqual(before, after)

    def test_missing_core_command_fails_before_any_project_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-missing-core-") as name:
            repository = Path(name)
            with self.assertRaises(FileNotFoundError):
                subprocess.run(
                    ["cw", "mcp", "serve", "--allowed-root", ".", "--project", "."],
                    cwd=repository, env={"PATH": ""}, check=False,
                )
            self.assertFalse((repository / ".cw").exists())


class PluginZipExtractionTests(unittest.TestCase):
    def test_canonical_zip_prepares_an_atomic_local_marketplace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-extract-") as name:
            root = Path(name)
            archive = root / "cw-plugin-0.1.0.zip"
            metadata = build(archive)
            destination = root / "marketplace"
            result = prepare_marketplace(
                archive, destination, expected_sha256=str(metadata["sha256"]),
            )
            self.assertEqual("0.1.0", result["plugin_version"])
            self.assertEqual((destination / PLUGIN_RELATIVE).resolve(),
                             validate_marketplace_root(destination, require_legal=True))
            self.assertEqual((ROOT / "LICENSE").read_bytes(),
                             (destination / PLUGIN_RELATIVE / "LICENSE").read_bytes())
            self.assertEqual((ROOT / "NOTICE").read_bytes(),
                             (destination / PLUGIN_RELATIVE / "NOTICE").read_bytes())
            for path in (destination / PLUGIN_RELATIVE).rglob("*"):
                self.assertFalse(path.is_symlink())
                if path.is_file():
                    self.assertEqual(0, path.stat().st_mode & 0o111)
                    self.assertEqual(1, path.stat().st_nlink)

    def test_changed_immutable_fixture_preserves_previous_marketplace_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-fixture-upgrade-") as name:
            root = Path(name)
            first_archive = root / "first.zip"
            first = build(first_archive)
            first_marketplace = root / "first-marketplace"
            prepare_marketplace(
                first_archive, first_marketplace, expected_sha256=str(first["sha256"]),
            )
            alternate = root / "alternate-source"
            shutil.copytree(ROOT, alternate, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".pytest_cache", "build", "dist", "site", "artifacts",
            ))
            readme = alternate / PLUGIN_RELATIVE / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + "\nImmutable evaluation fixture B.\n",
                              encoding="utf-8")
            second_archive = root / "second.zip"
            second = build(second_archive, root=alternate)
            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["plugin_version"], second["plugin_version"])
            second_marketplace = root / "second-marketplace"
            prepare_marketplace(
                second_archive, second_marketplace,
                expected_sha256=str(second["sha256"]), root=alternate,
            )
            self.assertEqual("0.1.0", (first_marketplace / PLUGIN_RELATIVE / "VERSION").read_text().strip())
            self.assertNotIn("fixture B", (first_marketplace / PLUGIN_RELATIVE / "README.md").read_text())
            self.assertIn("fixture B", (second_marketplace / PLUGIN_RELATIVE / "README.md").read_text())

    def test_hash_mismatch_and_existing_destination_preserve_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-atomic-") as name:
            root = Path(name)
            archive = root / "candidate.zip"
            metadata = build(archive)
            destination = root / "existing"
            destination.mkdir()
            marker = destination / "preserved"
            marker.write_text("previous", encoding="utf-8")
            for digest in ("0" * 64, str(metadata["sha256"])):
                with self.subTest(digest=digest), self.assertRaises(DistributionError):
                    prepare_marketplace(archive, destination, expected_sha256=digest)
                self.assertEqual("previous", marker.read_text(encoding="utf-8"))

            interrupted = root / "interrupted"
            with patch("scripts.prepare_plugin_marketplace.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    prepare_marketplace(
                        archive, interrupted, expected_sha256=str(metadata["sha256"]),
                    )
            self.assertFalse(interrupted.exists())
            self.assertFalse(any(path.name.startswith(".cw-plugin-marketplace-") for path in root.iterdir()))

    def test_unsafe_and_corrupt_archives_are_rejected_without_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-unsafe-") as name:
            root = Path(name)
            canonical = root / "canonical.zip"
            build(canonical)
            cases = {
                "traversal": ("../escape", 0o100644),
                "absolute": ("/absolute", 0o100644),
                "windows-drive": ("C:/escape", 0o100644),
                "backslash": ("cw\\escape", 0o100644),
                "symlink": ("cw/link", 0o120777),
                "fifo": ("cw/fifo", 0o010644),
                "device": ("cw/device", 0o060644),
                "executable": ("cw/run", 0o100755),
                "case-collision": ("cw/license", 0o100644),
            }
            for label, (extra, mode) in cases.items():
                target = root / f"{label}.zip"
                with zipfile.ZipFile(canonical) as source, zipfile.ZipFile(target, "w") as output:
                    for entry in source.infolist():
                        output.writestr(entry, source.read(entry.filename))
                    info = zipfile.ZipInfo(extra, FIXED_TIME)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = mode << 16
                    output.writestr(info, b"payload")
                with self.subTest(label=label), self.assertRaises(DistributionError):
                    prepare_marketplace(
                        target, root / f"out-{label}",
                        expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    )
                self.assertFalse((root / f"out-{label}").exists())

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as output:
                info = zipfile.ZipInfo("cw/VERSION", FIXED_TIME)
                info.external_attr = 0o100644 << 16
                output.writestr(info, b"0.1.0")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    output.writestr(info, b"0.1.0")
            with self.assertRaises(DistributionError):
                prepare_marketplace(
                    duplicate, root / "out-duplicate",
                    expected_sha256=hashlib.sha256(duplicate.read_bytes()).hexdigest(),
                )

            corrupt = root / "corrupt.zip"
            corrupt.write_bytes(b"not-a-zip")
            with self.assertRaises(DistributionError):
                prepare_marketplace(
                    corrupt, root / "out-corrupt",
                    expected_sha256=hashlib.sha256(corrupt.read_bytes()).hexdigest(),
                )


@unittest.skipUnless(shutil.which("codex"), "official Codex CLI is not installed")
class CodexPluginInstallationTests(unittest.TestCase):
    def run_codex(self, codex_home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "CODEX_HOME": str(codex_home)}
        return subprocess.run(
            ["codex", *arguments], env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
        )

    def test_local_add_install_remove_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-codex-local-") as name:
            codex_home = Path(name) / "codex"
            codex_home.mkdir()
            added = self.run_codex(codex_home, "plugin", "marketplace", "add", str(ROOT), "--json")
            self.assertEqual(0, added.returncode, added.stderr)
            listing = self.run_codex(codex_home, "plugin", "list", "--marketplace", "cw-development",
                                     "--available", "--json")
            self.assertEqual(0, listing.returncode, listing.stderr)
            available = json.loads(listing.stdout)["available"]
            self.assertEqual(["cw@cw-development"], [item["pluginId"] for item in available])
            installed = self.run_codex(codex_home, "plugin", "add", "cw@cw-development", "--json")
            self.assertEqual(0, installed.returncode, installed.stderr)
            payload = json.loads(installed.stdout)
            installed_path = Path(payload["installedPath"])
            self.assertEqual("0.1.0", payload["version"])
            self.assertTrue((installed_path / "skills/cw-workflow/SKILL.md").is_file())
            servers = self.run_codex(codex_home, "mcp", "list", "--json")
            self.assertEqual(0, servers.returncode, servers.stderr)
            mcp = json.loads(servers.stdout)
            self.assertEqual(["cw"], [item["name"] for item in mcp])
            self.assertEqual("stdio", mcp[0]["transport"]["type"])
            removed = self.run_codex(codex_home, "plugin", "remove", "cw@cw-development", "--json")
            self.assertEqual(0, removed.returncode, removed.stderr)
            source_removed = self.run_codex(
                codex_home, "plugin", "marketplace", "remove", "cw-development", "--json",
            )
            self.assertEqual(0, source_removed.returncode, source_removed.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("cw-development", config)
            cache = codex_home / "plugins/cache/cw-development"
            self.assertFalse(any(path.is_file() or path.is_symlink() for path in cache.rglob("*")))
            repeated = self.run_codex(codex_home, "plugin", "remove", "cw@cw-development", "--json")
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual("cw@cw-development", json.loads(repeated.stdout)["pluginId"])
            repeated_source = self.run_codex(
                codex_home, "plugin", "marketplace", "remove", "cw-development", "--json",
            )
            self.assertNotEqual(0, repeated_source.returncode)
            self.assertNotIn("Traceback", repeated_source.stderr)

    def test_safely_extracted_zip_is_a_supported_local_marketplace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-codex-zip-") as name:
            root = Path(name)
            archive = root / "candidate.zip"
            metadata = build(archive)
            marketplace = root / "marketplace"
            prepare_marketplace(archive, marketplace, expected_sha256=str(metadata["sha256"]))
            codex_home = root / "codex"
            codex_home.mkdir()
            added = self.run_codex(
                codex_home, "plugin", "marketplace", "add", str(marketplace), "--json",
            )
            self.assertEqual(0, added.returncode, added.stderr)
            installed = self.run_codex(codex_home, "plugin", "add", "cw@cw-development", "--json")
            self.assertEqual(0, installed.returncode, installed.stderr)
            installed_path = Path(json.loads(installed.stdout)["installedPath"])
            self.assertTrue((installed_path / "LICENSE").is_file())
            self.assertTrue((installed_path / "NOTICE").is_file())
            self.assertEqual(0, self.run_codex(
                codex_home, "plugin", "remove", "cw@cw-development", "--json",
            ).returncode)
            self.assertEqual(0, self.run_codex(
                codex_home, "plugin", "marketplace", "remove", "cw-development", "--json",
            ).returncode)

    @unittest.skipUnless(os.environ.get("CW_TEST_GIT_MARKETPLACE_REF"),
                         "immutable Git marketplace acceptance is opt-in")
    def test_immutable_git_marketplace_and_same_ref_upgrade(self) -> None:
        reference = os.environ["CW_TEST_GIT_MARKETPLACE_REF"]
        self.assertRegex(reference, r"^[0-9a-f]{40}$")
        with tempfile.TemporaryDirectory(prefix="cw-plugin-codex-git-") as name:
            codex_home = Path(name) / "codex"
            codex_home.mkdir()
            added = self.run_codex(
                codex_home, "plugin", "marketplace", "add", "Queopius/cw",
                "--ref", reference, "--sparse", ".agents/plugins", "--sparse", "plugins/cw", "--json",
            )
            self.assertEqual(0, added.returncode, added.stderr)
            before = self.run_codex(codex_home, "plugin", "list", "--marketplace", "cw-development",
                                    "--available", "--json")
            self.assertEqual(0, before.returncode, before.stderr)
            upgraded = self.run_codex(
                codex_home, "plugin", "marketplace", "upgrade", "cw-development", "--json",
            )
            self.assertEqual(0, upgraded.returncode, upgraded.stderr)
            after = self.run_codex(codex_home, "plugin", "list", "--marketplace", "cw-development",
                                   "--available", "--json")
            self.assertEqual(json.loads(before.stdout), json.loads(after.stdout))
            self.assertEqual(0, self.run_codex(
                codex_home, "plugin", "marketplace", "remove", "cw-development", "--json",
            ).returncode)

    @unittest.skipUnless(os.environ.get("CW_TEST_GIT_MARKETPLACE_REF"),
                         "immutable Git marketplace acceptance is opt-in")
    def test_missing_ref_and_inaccessible_repository_leave_no_partial_source(self) -> None:
        cases = (
            ("Queopius/cw", ("--ref", "0" * 40)),
            ("https://127.0.0.1:1/Queopius/cw.git", ("--ref", os.environ["CW_TEST_GIT_MARKETPLACE_REF"])),
        )
        with tempfile.TemporaryDirectory(prefix="cw-plugin-codex-git-negative-") as name:
            root = Path(name)
            for index, (source, options) in enumerate(cases):
                codex_home = root / str(index)
                codex_home.mkdir()
                failed = self.run_codex(
                    codex_home, "plugin", "marketplace", "add", source, *options,
                    "--sparse", ".agents/plugins", "--sparse", "plugins/cw", "--json",
                )
                self.assertNotEqual(0, failed.returncode)
                listing = self.run_codex(codex_home, "plugin", "marketplace", "list", "--json")
                self.assertEqual(0, listing.returncode, listing.stderr)
                names = {item["name"] for item in json.loads(listing.stdout)["marketplaces"]}
                self.assertNotIn("cw-development", names)
                config = codex_home / "config.toml"
                if config.exists():
                    self.assertNotIn("cw-development", config.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
