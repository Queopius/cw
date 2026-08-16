from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from cw import __version__

from cw.remote.deployment import GatewayDeploymentConfig
from cw.remote.protocol import PROTOCOL_VERSION
from scripts.validate_staging_bootstrap import validation_errors
from scripts.validate_plugin_candidate import PLUGIN


HAS_REMOTE = __import__("importlib").util.find_spec("mcp") is not None
SHA = "a" * 40


def environment(database: Path) -> dict[str, str]:
    return {
        "CW_DEPLOYMENT_ENV": "staging",
        "RENDER_GIT_COMMIT": SHA,
        "CW_GATEWAY_RESOURCE_URL": "https://staging-mcp.cwcli.dev/mcp",
        "CW_GATEWAY_DATABASE": str(database),
        "CW_GATEWAY_ALLOWED_HOSTS": "staging-mcp.cwcli.dev,cw-staging-mcp.onrender.com",
        "CW_OAUTH_ISSUER_URL": "https://tenant.eu.auth0.com/",
        "CW_OAUTH_JWKS_URL": "https://tenant.eu.auth0.com/.well-known/jwks.json",
        "CW_OAUTH_WORKSPACE_CLAIM": "https://cwcli.dev/claims/workspace",
        "CW_OAUTH_ALGORITHMS": "RS256",
        "PORT": "10000",
    }


class StagingConfigurationTests(unittest.TestCase):
    def test_render_environment_is_typed_and_uses_exact_build_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = GatewayDeploymentConfig.from_environment(
                environment(Path(directory) / "gateway.sqlite3")
            )
        self.assertEqual("staging", config.environment)
        self.assertEqual(SHA, config.build_sha)
        self.assertEqual((PLUGIN / "VERSION").read_text(encoding="utf-8").strip(), config.plugin_version)
        self.assertEqual("https://staging-mcp.cwcli.dev/mcp", config.oauth.resource)
        self.assertEqual("https://tenant.eu.auth0.com/", config.oauth.issuer)
        self.assertEqual(("RS256",), config.oauth.algorithms)
        self.assertEqual(10000, config.port)
        self.assertEqual(4, config.limits.concurrent_requests_per_device)

    def test_explicit_cw_build_sha_overrides_render_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = environment(Path(directory) / "gateway.sqlite3")
            values["CW_BUILD_SHA"] = "b" * 40
            config = GatewayDeploymentConfig.from_environment(values)
        self.assertEqual("b" * 40, config.build_sha)

    def test_invalid_or_unsafe_deployment_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = environment(Path(directory) / "gateway.sqlite3")
            cases = (
                {**base, "RENDER_GIT_COMMIT": "short"},
                {**base, "CW_GATEWAY_RESOURCE_URL": "http://staging-mcp.cwcli.dev/mcp"},
                {**base, "CW_GATEWAY_DATABASE": "relative.sqlite3"},
                {**base, "CW_GATEWAY_ALLOWED_HOSTS": "https://staging-mcp.cwcli.dev"},
                {**base, "CW_LIMIT_REQUESTS_PER_MINUTE": "0"},
                {key: value for key, value in base.items() if key != "CW_OAUTH_ISSUER_URL"},
            )
            for values in cases:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    GatewayDeploymentConfig.from_environment(values)

    def test_machine_readable_environment_contract_contains_no_gateway_secret(self) -> None:
        root = Path(__file__).resolve().parents[1]
        payload = json.loads((root / "config/staging-environment.json").read_text(encoding="utf-8"))
        self.assertEqual([], payload["gateway_secrets"])
        self.assertFalse(any(item["secret"] for item in payload["variables"]))

    def test_static_staging_contract_validator_passes(self) -> None:
        self.assertEqual([], validation_errors())


@unittest.skipUnless(HAS_REMOTE, "remote dependencies unavailable")
class StagingRuntimeTests(unittest.TestCase):
    def test_readiness_exposes_only_sanitized_build_identity(self) -> None:
        from starlette.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            config = GatewayDeploymentConfig.from_environment(
                environment(Path(directory) / "gateway.sqlite3")
            )
            app, store = config.create_app()
            try:
                with TestClient(app, base_url="http://staging-mcp.cwcli.dev") as client:
                    health = client.get("/healthz")
                    readiness = client.get("/readyz")
                    self.assertEqual(200, health.status_code)
                    self.assertEqual(200, readiness.status_code)
                    self.assertEqual(SHA, readiness.json()["build"]["build_sha"])
                    self.assertEqual(__version__, readiness.json()["build"]["cw_core_version"])
                    self.assertEqual((PLUGIN / "VERSION").read_text(encoding="utf-8").strip(), readiness.json()["build"]["cw_plugin_version"])
                    self.assertEqual(PROTOCOL_VERSION, readiness.json()["build"]["protocol_version"])
                    self.assertEqual(PROTOCOL_VERSION, readiness.json()["build"]["remote_protocol_version"])
                    encoded = json.dumps(readiness.json())
                    self.assertNotIn(str(Path.home()), encoded)
                    self.assertNotIn("tenant.eu.auth0.com", encoded)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
