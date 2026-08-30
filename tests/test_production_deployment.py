from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from cw import __version__
from cw.remote.deployment import GatewayDeploymentConfig
from cw.remote.protocol import PROTOCOL_VERSION, all_remote_scopes
from scripts.validate_plugin_candidate import PLUGIN
from scripts.validate_production_deployment import EXPECTED_VALUES, validation_errors


HAS_REMOTE = __import__("importlib").util.find_spec("mcp") is not None
SHA = "c" * 40
RENDER_HOST = "cw-mcp-abc123.onrender.com"


def environment() -> dict[str, str]:
    return {
        **EXPECTED_VALUES,
        "RENDER_GIT_COMMIT": SHA,
        "CW_GATEWAY_ALLOWED_HOSTS": f"mcp.cwcli.dev,{RENDER_HOST}",
        "CW_PAIRING_WEB_CLIENT_ID": "production-public-pairing-client",
        "CW_PAIRING_SESSION_SECRET": "p" * 32,
        "PORT": "10000",
    }


class ProductionConfigurationTests(unittest.TestCase):
    def test_static_production_contract_validator_passes(self) -> None:
        self.assertEqual([], validation_errors())

    def test_exact_production_environment_is_accepted(self) -> None:
        config = GatewayDeploymentConfig.from_environment(environment())
        self.assertEqual("production", config.environment)
        self.assertEqual(SHA, config.build_sha)
        self.assertEqual("0.1.0", config.plugin_version)
        self.assertEqual(Path("/var/lib/cw/gateway.sqlite3"), config.database)
        self.assertEqual("https://mcp.cwcli.dev/mcp", config.oauth.resource)
        self.assertEqual("https://auth.cwcli.dev/", config.oauth.issuer)
        self.assertEqual("https://auth.cwcli.dev/.well-known/jwks.json", config.oauth.jwks_uri)
        self.assertEqual("https://cwcli.dev/claims/workspace", config.oauth.workspace_claim)
        self.assertEqual(("RS256",), config.oauth.algorithms)
        self.assertEqual(("mcp.cwcli.dev", RENDER_HOST), config.allowed_hosts)
        self.assertIsNotNone(config.pairing_web)
        assert config.pairing_web is not None
        self.assertEqual("https://mcp.cwcli.dev/remote/pair/callback", config.pairing_web.redirect_uri)
        self.assertEqual(("project.read",), config.pairing_web.scopes)
        self.assertEqual(120, config.limits.requests_per_minute)
        self.assertEqual(240, config.limits.requests_per_device_per_minute)
        self.assertEqual(20, config.limits.pairing_requests_per_minute)
        self.assertEqual(4, config.limits.concurrent_requests_per_device)
        self.assertEqual(65536, config.limits.maximum_request_bytes)
        self.assertEqual(524288, config.limits.maximum_agent_message_bytes)
        self.assertEqual(30.0, config.limits.operation_timeout_seconds)
        self.assertEqual(45.0, config.limits.agent_idle_seconds)
        self.assertEqual(1024, config.limits.completed_response_cache_size)

    def test_missing_or_drifted_production_values_fail_closed(self) -> None:
        base = environment()
        cases = []
        for name in EXPECTED_VALUES:
            cases.append({key: value for key, value in base.items() if key != name})
        cases.extend((
            {**base, "CW_GATEWAY_RESOURCE_URL": "https://staging-mcp.cwcli.dev/mcp"},
            {**base, "CW_OAUTH_ISSUER_URL": "https://login.cwcli.dev/"},
            {**base, "CW_GATEWAY_DATABASE": "/var/lib/cw-staging/gateway.sqlite3"},
            {**base, "CW_GATEWAY_ALLOWED_HOSTS": "mcp.cwcli.dev,cw-staging-mcp.onrender.com"},
            {**base, "CW_GATEWAY_ALLOWED_HOSTS": "mcp.cwcli.dev"},
            {**base, "CW_GATEWAY_ALLOWED_HOSTS": "mcp.cwcli.dev,*.onrender.com"},
            {**base, "CW_GATEWAY_ALLOWED_HOSTS": f"mcp.cwcli.dev,{RENDER_HOST},extra.example.com"},
            {**base, "CW_OAUTH_ALGORITHMS": "RS256,HS256"},
            {key: value for key, value in base.items() if key != "CW_PAIRING_WEB_CLIENT_ID"},
            {key: value for key, value in base.items() if key != "CW_PAIRING_SESSION_SECRET"},
        ))
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                GatewayDeploymentConfig.from_environment(values)

    def test_staging_contract_does_not_inherit_production_constraints(self) -> None:
        values = environment()
        values.update({
            "CW_DEPLOYMENT_ENV": "staging",
            "CW_GATEWAY_RESOURCE_URL": "https://staging-mcp.cwcli.dev/mcp",
            "CW_GATEWAY_DATABASE": "/var/lib/cw/gateway.sqlite3",
            "CW_GATEWAY_ALLOWED_HOSTS": "staging-mcp.cwcli.dev,cw-staging-mcp.onrender.com",
            "CW_OAUTH_ISSUER_URL": "https://login.cwcli.dev/",
            "CW_OAUTH_JWKS_URL": "https://login.cwcli.dev/.well-known/jwks.json",
            "CW_PAIRING_WEB_REDIRECT_URI": "https://staging-mcp.cwcli.dev/remote/pair/callback",
        })
        self.assertEqual("staging", GatewayDeploymentConfig.from_environment(values).environment)


@unittest.skipUnless(HAS_REMOTE, "remote dependencies unavailable")
class ProductionRuntimeTests(unittest.TestCase):
    def test_health_readiness_and_oauth_metadata_are_exact(self) -> None:
        from starlette.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            config = GatewayDeploymentConfig.from_environment(environment())
            config = dataclasses.replace(config, database=Path(directory) / "gateway.sqlite3")
            app, store = config.create_app()
            try:
                with TestClient(app, base_url="https://mcp.cwcli.dev") as client:
                    health = client.get("/healthz")
                    ready = client.get("/readyz")
                    anonymous = client.get("/mcp")
                    metadata = client.get("/.well-known/oauth-protected-resource")
                self.assertEqual(200, health.status_code)
                self.assertEqual("ok", health.json()["status"])
                self.assertEqual("cw-remote-gateway", health.json()["service"])
                self.assertEqual("production", health.json()["build"]["environment"])
                self.assertEqual(200, ready.status_code)
                self.assertEqual(1, ready.json()["schema_version"])
                self.assertEqual("production", ready.json()["build"]["environment"])
                self.assertEqual(__version__, ready.json()["build"]["cw_core_version"])
                self.assertEqual("0.1.0", ready.json()["build"]["cw_plugin_version"])
                self.assertEqual(PROTOCOL_VERSION, ready.json()["build"]["remote_protocol_version"])
                self.assertEqual(SHA, ready.json()["build"]["build_sha"])
                self.assertEqual(401, anonymous.status_code)
                self.assertIn("resource_metadata=", anonymous.headers["www-authenticate"])
                self.assertEqual(200, metadata.status_code)
                payload = metadata.json()
                self.assertEqual("https://mcp.cwcli.dev/mcp", payload["resource"])
                self.assertEqual(["https://auth.cwcli.dev/"], payload["authorization_servers"])
                self.assertEqual(set(all_remote_scopes()), set(payload["scopes_supported"]))
                self.assertNotIn("workflow.admin", payload["scopes_supported"])
                self.assertNotIn("staging", json.dumps({"health": health.json(), "ready": ready.json(), "metadata": payload}).lower())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
