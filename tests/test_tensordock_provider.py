from __future__ import annotations

from typing import Any, Mapping

import unittest

from deployment import Deployment
from deployment.providers import TensorDockProvider


class DummyTensorDockAPI:
    def __init__(self, response: Mapping[str, Any], *, updates: list[Mapping[str, Any]] | None = None) -> None:
        self.response = response
        self.updates = updates or []
        self.calls: list[Mapping[str, Any]] = []
        self.get_calls: list[str] = []

    def create_instance(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(dict(payload))
        return self.response

    def get_instance(self, instance_id: str) -> Mapping[str, Any]:
        self.get_calls.append(instance_id)
        if self.updates:
            return self.updates.pop(0)
        return self.response


class TensorDockProviderTestCase(unittest.TestCase):
    def test_provision_uses_real_ip(self) -> None:
        api_response = {"instance": {"ipAddress": "203.0.113.10"}}
        provider = TensorDockProvider(DummyTensorDockAPI(api_response))
        deployment = Deployment(name="gonka")

        result = provider.provision(deployment, {"HOST": "example"})

        self.assertEqual(deployment.ip_address, "203.0.113.10")
        self.assertIn("203.0.113.10", result.cloud_init)
        self.assertIn("KEY_NAME=node-gonka-203.0.113.10", result.config_env)

    def test_config_env_preserves_uppercase_payload(self) -> None:
        api_response = {"ipAddress": "198.51.100.42"}
        provider = TensorDockProvider(DummyTensorDockAPI(api_response))
        deployment = Deployment(name="race")

        result = provider.provision(deployment, {"HOST": "example", "port": 22})

        self.assertIn("HOST=example", result.config_env)
        self.assertNotIn("port=22", result.config_env)
        self.assertEqual(result.instance_data["ipAddress"], "198.51.100.42")

    def test_waits_until_ip_available(self) -> None:
        api_response = {"instanceId": "abc123"}
        updates = [{"instance": {"ipAddress": "192.0.2.55"}}]
        provider = TensorDockProvider(
            DummyTensorDockAPI(api_response, updates=updates),
            poll_interval=0.01,
            poll_timeout=1.0,
        )
        deployment = Deployment(name="wait")

        result = provider.provision(deployment, {})

        self.assertEqual(deployment.ip_address, "192.0.2.55")
        self.assertIn("192.0.2.55", result.cloud_init)
        self.assertEqual(provider._api_client.get_calls, ["abc123"])


if __name__ == "__main__":
    unittest.main()
