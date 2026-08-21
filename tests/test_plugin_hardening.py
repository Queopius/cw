from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cw.adapters.mcp.compatibility import ensure_plugin_compatible, load_plugin_compatibility
from cw.adapters.mcp.runtime import MCPRuntime, RuntimeConfig, TOOLS
from cw.application import ApplicationError, ApplicationErrorCode
from cw.remote.agent import HTTPAgentClient, register_project_grant, request_pairing, validate_gateway_url
from cw.remote.device import DeviceCredential
from cw.update.cache import _manifest_dict
from cw.update.models import PluginRelease, ReleaseManifest
from scripts.build_plugin_candidate import FIXED_TIME, build, validate_archive
from scripts.validate_plugin_candidate import ROOT, validation_errors
from tests.helpers import TempRepo


class RemoteOriginHardeningTests(unittest.TestCase):
    def test_loopback_policy_is_structural_and_explicit(self) -> None:
        accepted = {
            "http://127.0.0.1": "http://127.0.0.1",
            "http://[::1]:8765": "http://[::1]:8765",
            "https://gateway.example:443/": "https://gateway.example:443",
        }
        for value, expected in accepted.items():
            self.assertEqual(expected, validate_gateway_url(value))
        for value in (
            "http://127.0.0.1@evil.example", "http://127.0.0.1.evil.example",
            "http://127.0.0.10", "http://localhost.evil.example",
            "http://[::1]@evil.example", "http://localhost", "http://localhost.",
            "http://192.168.1.1", "ftp://127.0.0.1", "https://gateway.example/path",
            "http://127.0.0.1:99999", "http://127.0.0.1:not-a-port",
            "https://gateway.example.", "https://127.0.0.1", "https://169.254.169.254",
            "https://10.0.0.1", "https://2130706433", "https://0x7f000001",
            "https://g\N{LATIN SMALL LETTER A WITH ACUTE}teway.example",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_gateway_url(value)

    @unittest.skipUnless(importlib.util.find_spec("httpx"), "Remote extra is not installed")
    def test_all_remote_clients_disable_redirects_and_validate_before_io(self) -> None:
        credential = DeviceCredential.generate()
        repo = TempRepo(name="remote-origin")

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {}

        class Client:
            options: list[dict] = []

            def __init__(self, **kwargs):
                self.options.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return Response()

        try:
            with patch("httpx.AsyncClient", Client):
                asyncio.run(request_pairing(
                    gateway_url="https://gateway.example", credential=credential, display_name="test",
                ))
                with self.assertRaises(Exception):
                    asyncio.run(register_project_grant(
                        gateway_url="https://gateway.example", credential=credential, project=repo.root,
                    ))
                agent = HTTPAgentClient(
                    gateway_url="https://gateway.example", credential=credential,
                    runtime=object(),  # type: ignore[arg-type]
                )
                stopped = asyncio.Event()
                stopped.set()
                asyncio.run(agent.run(stopped))
            self.assertTrue(Client.options)
            self.assertTrue(all(item.get("follow_redirects") is False for item in Client.options))
            self.assertTrue(all(item.get("trust_env") is False for item in Client.options))
            for function in (request_pairing, register_project_grant):
                with self.assertRaises(ValueError):
                    asyncio.run(function(**({
                        "gateway_url": "http://127.0.0.1@evil.example", "credential": credential,
                        "display_name": "test",
                    } if function is request_pairing else {
                        "gateway_url": "http://127.0.0.1@evil.example", "credential": credential,
                        "project": repo.root,
                    })))
        finally:
            repo.close()


class PluginCompatibilityTests(unittest.TestCase):
    def test_supported_and_unsupported_core_versions_fail_closed(self) -> None:
        for value in ("0.14.0", "0.14.1", "0.99.99"):
            self.assertEqual("0.1.0", ensure_plugin_compatible(core_version=value)["plugin_version"])
        for value in ("0.13.99", "1.0.0", "2.0.0", "bad", ""):
            with self.subTest(value=value), self.assertRaises(ApplicationError) as raised:
                ensure_plugin_compatible(core_version=value)
            self.assertEqual(ApplicationErrorCode.PLATFORM_CAPABILITY_UNAVAILABLE, raised.exception.code)
        with self.assertRaises(ApplicationError):
            ensure_plugin_compatible(core_version="0.14.1", plugin_version="0.14.1")

    def test_missing_or_manipulated_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = root / "missing.json"
            with self.assertRaises(ApplicationError):
                load_plugin_compatibility(missing)
            canonical = load_plugin_compatibility()
            for mutation in (
                lambda value: value.update(plugin_version="9.9.9"),
                lambda value: value["core"].update(maximum_exclusive="not-a-version"),
                lambda value: value.update(unexpected=True),
            ):
                value = copy.deepcopy(canonical)
                mutation(value)
                path = root / "policy.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ApplicationError):
                    ensure_plugin_compatible(core_version="0.14.1", policy_path=path)


class MCPContractHardeningTests(unittest.TestCase):
    def test_exact_tool_surface_has_closed_versioned_schemas_and_golden_hash(self) -> None:
        self.assertEqual(12, len(TOOLS))
        schemas = {}
        for contract in TOOLS:
            input_schema = contract.input_schema()
            output_schema = contract.output_schema()
            self.assertIs(input_schema["additionalProperties"], False)
            self.assertIs(output_schema["additionalProperties"], False)
            self.assertEqual(1, output_schema["properties"]["schema_version"]["const"])
            schemas[contract.name] = {"input": input_schema, "output": output_schema}
            self.assertEqual(not contract.mutation, contract.to_dict()["annotations"]["idempotentHint"])
        encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual("07c4e1f49fe60b3a3dd54f7c13f3f358f01e687deaa1fb7b5e47fb9641f73c91", hashlib.sha256(encoded).hexdigest())

    def test_runtime_rejects_unknown_arguments_and_invalid_output(self) -> None:
        repo = TempRepo(name="schema-runtime")
        runtime = MCPRuntime(RuntimeConfig.create([repo.root]), diagnostic_sink=lambda _: None)
        try:
            handle = runtime.project_handles()[0]["repository_id"]
            unknown = runtime.call_tool("cw_project_status", {"project_id": handle, "extra": "smuggled"})
            self.assertEqual("INVALID_REQUEST", unknown["error"]["code"])
            for arguments in (
                {"project_id": 7},
                {"project_id": handle, "operation_id": "é"},
                {"project_id": handle, "operation_id": "a" * 129},
            ):
                malformed = runtime.call_tool("cw_project_status", arguments)
                self.assertEqual("INVALID_REQUEST", malformed["error"]["code"])
            empty = runtime.call_tool("cw_project_status", {"project_id": "", "operation_id": ""})
            self.assertEqual("SUCCEEDED", empty["status"])

            class InvalidResult:
                def to_dict(self):
                    return {"schema_version": 1, "operation_id": "invalid-output", "status": "SUCCEEDED", "secret": "x"}

            with patch.object(runtime.application, "status", return_value=InvalidResult()):
                invalid = runtime.call_tool("cw_project_status", {"project_id": handle})
            self.assertEqual("STATE_INCONSISTENT", invalid["error"]["code"])
            self.assertNotIn("secret", json.dumps(invalid))
        finally:
            runtime.shutdown()
            repo.close()

    def test_missing_operation_id_is_explicitly_non_idempotent_for_mutations(self) -> None:
        repo = TempRepo(name="missing-operation-id")
        runtime = MCPRuntime(RuntimeConfig.create([repo.root]), diagnostic_sink=lambda _: None)
        try:
            handle = runtime.project_handles()[0]["repository_id"]
            first = runtime.call_tool("cw_phase_start", {"project_id": handle})
            second = runtime.call_tool("cw_phase_start", {"project_id": handle})
            self.assertNotEqual(first["operation_id"], second["operation_id"])
            mutation = next(item for item in runtime.tool_contracts() if item["name"] == "cw_phase_start")
            self.assertFalse(mutation["annotations"]["idempotentHint"])
        finally:
            runtime.shutdown()
            repo.close()

    def test_concurrent_retry_with_same_id_creates_one_operation(self) -> None:
        repo = TempRepo(name="concurrent-operation-id")
        runtime = MCPRuntime(RuntimeConfig.create([repo.root]), diagnostic_sink=lambda _: None)
        responses: list[dict] = []
        try:
            handle = runtime.project_handles()[0]["repository_id"]
            barrier = threading.Barrier(2)

            def invoke() -> None:
                barrier.wait()
                responses.append(runtime.call_tool("cw_phase_start", {
                    "project_id": handle, "operation_id": "concurrent-same-id",
                }))

            workers = [threading.Thread(target=invoke) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
            self.assertEqual(2, len(responses))
            self.assertEqual({"concurrent-same-id"}, {item["operation_id"] for item in responses})
            self.assertTrue(any(item.get("idempotent_replay") for item in responses))
            records = list((repo.root / ".cw/runtime/operations").glob("*.json"))
            self.assertEqual(1, len(records))
        finally:
            runtime.shutdown()
            repo.close()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP extra is not installed")
    def test_announced_python_and_runtime_contracts_match(self) -> None:
        from cw.adapters.mcp.server import create_server

        repo = TempRepo(name="schema-sdk")
        runtime = MCPRuntime(RuntimeConfig.create([repo.root]), diagnostic_sink=lambda _: None)
        try:
            server = create_server(runtime)
            for tool in server._tool_manager.list_tools():
                contract = runtime.tool_contract(tool.name)
                self.assertEqual(contract.input_schema(), tool.parameters)
                self.assertEqual(contract.output_schema(), tool.fn_metadata.output_schema)
                self.assertEqual(not contract.mutation, bool(tool.annotations.idempotentHint))
                if tool.name == "cw_project_status":
                    with self.assertRaises(Exception):
                        asyncio.run(tool.run({"unknown": "smuggled"}))
                    output = asyncio.run(tool.run({}))
                    self.assertEqual(1, output["schema_version"])
                    self.assertEqual("SUCCEEDED", output["status"])
        finally:
            runtime.shutdown()
            repo.close()


class PluginPackagingHardeningTests(unittest.TestCase):
    def test_legal_materials_are_canonical_and_builds_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            first = Path(name) / "first.zip"
            second = Path(name) / "second.zip"
            one = build(first)
            two = build(second)
            self.assertEqual(one["sha256"], two["sha256"])
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual((ROOT / "LICENSE").read_bytes(), archive.read("cw/LICENSE"))
                self.assertEqual((ROOT / "NOTICE").read_bytes(), archive.read("cw/NOTICE"))

    def test_manipulated_archives_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            canonical = root / "canonical.zip"
            build(canonical)
            mutations = {
                "missing-license": None,
                "traversal": "../escape",
                "case-collision": "cw/license",
                "symlink": "cw/link",
            }
            for label, extra in mutations.items():
                target = root / f"{label}.zip"
                with zipfile.ZipFile(canonical) as source, zipfile.ZipFile(target, "w") as output:
                    for entry in source.infolist():
                        if label == "missing-license" and entry.filename == "cw/LICENSE":
                            continue
                        output.writestr(entry, source.read(entry.filename))
                    if extra is not None:
                        info = zipfile.ZipInfo(extra, FIXED_TIME)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = (0o120777 if label == "symlink" else 0o100644) << 16
                        output.writestr(info, b"payload")
                self.assertTrue(validate_archive(target), label)
            corrupt = root / "corrupt.zip"
            corrupt.write_bytes(b"not a zip")
            self.assertTrue(validate_archive(corrupt))

    def test_alternate_root_version_is_authoritative_and_must_be_regular(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            alternate = Path(name) / "repo"
            shutil.copytree(ROOT, alternate, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", "build", "site", "*.egg-info",
            ))
            self.assertEqual([], validation_errors(alternate))
            version = alternate / "plugins/cw/VERSION"
            version.write_text("0.1.1\n", encoding="utf-8")
            errors = validation_errors(alternate)
            self.assertTrue(any("manifest base version" in item for item in errors))
            version.write_text("invalid\n", encoding="utf-8")
            self.assertTrue(any("semantic version" in item for item in validation_errors(alternate)))
            version.unlink()
            self.assertTrue(any("VERSION file is missing" in item for item in validation_errors(alternate)))
            version.symlink_to(alternate / "VERSION")
            self.assertTrue(any("non-symlink" in item for item in validation_errors(alternate)))


class PluginReleaseMetadataTests(unittest.TestCase):
    def plugin_value(self, archive: Path) -> dict:
        return {
            "name": "cw", "version": "0.1.0",
            "asset": {"filename": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size},
            "core_compatibility": {"minimum": "0.14.0", "maximum_exclusive": "1.0.0"},
            "provenance": {"source_commit": "abc123", "builder": "scripts/build_plugin_candidate.py"},
        }

    def test_plugin_metadata_validates_asset_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            archive = Path(name) / "cw-plugin-0.1.0.zip"
            archive.write_bytes(b"candidate")
            release = PluginRelease.from_dict(self.plugin_value(archive))
            release.validate_asset(archive)
            missing = Path(name) / "missing.zip"
            with self.assertRaises(Exception):
                release.validate_asset(missing)
            archive.write_bytes(b"tampered!")
            with self.assertRaises(Exception):
                release.validate_asset(archive)
            archive.write_bytes(b"changedxx")
            self.assertEqual(release.size, archive.stat().st_size)
            with self.assertRaises(Exception):
                release.validate_asset(archive)
            value = self.plugin_value(archive)
            value["unexpected"] = True
            with self.assertRaises(Exception):
                PluginRelease.from_dict(value)

    def test_old_and_new_release_manifests_are_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            archive = Path(name) / "cw-plugin-0.1.0.zip"
            archive.write_bytes(b"candidate")
            manifest = {
                "schema_version": 1, "version": "0.14.1", "channel": "stable",
                "published_at": "2026-08-21T00:00:00Z",
                "minimum_project_schema": 1, "maximum_project_schema": 1,
                "artifacts": [{
                    "platform": "linux", "arch": "x86_64", "url": "https://example.test/cw.tar.gz",
                    "sha256": "0" * 64, "filename": "cw.tar.gz",
                }],
                "release_notes": {"summary": "test", "url": "https://example.test/release"},
            }
            self.assertIsNone(ReleaseManifest.from_dict(manifest).plugin)
            manifest["signature"] = {"extensions": {"plugin": self.plugin_value(archive)}}
            legacy_allowed = {
                "schema_version", "version", "channel", "published_at",
                "minimum_project_schema", "maximum_project_schema", "artifacts",
                "release_notes", "signature",
            }
            self.assertFalse(set(manifest) - legacy_allowed)
            parsed = ReleaseManifest.from_dict(manifest)
            self.assertEqual("0.1.0", str(parsed.plugin.version))
            parsed.plugin.validate_asset(archive)
            cached = ReleaseManifest.from_dict(_manifest_dict(parsed))
            self.assertEqual(parsed.plugin, cached.plugin)
            for field, value in (("size", 0), ("sha256", "bad")):
                changed = copy.deepcopy(manifest)
                changed["signature"]["extensions"]["plugin"]["asset"][field] = value
                with self.assertRaises(Exception):
                    ReleaseManifest.from_dict(changed)
            changed = copy.deepcopy(manifest)
            changed["signature"]["extensions"]["plugin"]["version"] = "0.14.1"
            with self.assertRaises(Exception):
                ReleaseManifest.from_dict(changed)


if __name__ == "__main__":
    unittest.main()
