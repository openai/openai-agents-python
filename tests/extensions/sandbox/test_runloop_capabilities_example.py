from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest


def _load_example_module() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "sandbox"
        / "extensions"
        / "runloop"
        / "capabilities.py"
    )
    module_name = "tests.extensions.sandbox.runloop_capabilities_example"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeNotFoundError(Exception):
    def __init__(self) -> None:
        self.status_code = 404
        self.response = types.SimpleNamespace(status_code=404)


class _FakeConflictError(Exception):
    def __init__(self, message: str) -> None:
        self.status_code = 400
        self.response = types.SimpleNamespace(status_code=400)
        self.body = {"message": message}


class _FakeSecret:
    def __init__(self, name: str, secret_id: str) -> None:
        self.id = secret_id
        self.name = name


class _FakeSecretsClient:
    def __init__(self) -> None:
        self.secrets: dict[str, _FakeSecret] = {}
        self.create_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self._counter = 0

    def add(self, name: str) -> _FakeSecret:
        self._counter += 1
        secret = _FakeSecret(name=name, secret_id=f"secret-{self._counter}")
        self.secrets[name] = secret
        return secret

    async def get(self, name: str) -> _FakeSecret:
        if name not in self.secrets:
            raise _FakeNotFoundError()
        return self.secrets[name]

    async def create(self, *, name: str, value: str) -> _FakeSecret:
        self.create_calls.append((name, value))
        return self.add(name)


class _FakePolicy:
    def __init__(self, policy_id: str, name: str, description: str | None = None) -> None:
        self.id = policy_id
        self.name = name
        self.description = description


class _FakePolicyRef:
    def __init__(self, policy: _FakePolicy) -> None:
        self._policy = policy

    async def get_info(self) -> object:
        return types.SimpleNamespace(
            id=self._policy.id,
            name=self._policy.name,
            description=self._policy.description,
        )


class _FakeNetworkPoliciesClient:
    def __init__(self) -> None:
        self.policies: dict[str, _FakePolicy] = {}
        self.create_calls: list[dict[str, object]] = []
        self.delete_calls: list[str] = []
        self._counter = 0

    def add(self, name: str, description: str | None = None) -> _FakePolicy:
        self._counter += 1
        policy = _FakePolicy(
            policy_id=f"np-{self._counter}",
            name=name,
            description=description,
        )
        self.policies[policy.id] = policy
        return policy

    async def list(self, **params: object) -> list[_FakePolicy]:
        name = params.get("name")
        policies = list(self.policies.values())
        if isinstance(name, str):
            return [policy for policy in policies if policy.name == name]
        return policies

    async def create(self, **params: object) -> _FakePolicy:
        self.create_calls.append(dict(params))
        name = str(params["name"])
        if any(policy.name == name for policy in self.policies.values()):
            raise _FakeConflictError(f"NetworkPolicy with name '{name}' already exists")
        description = cast(
            str | None,
            params.get("description") if isinstance(params.get("description"), str) else None,
        )
        return self.add(
            name=name,
            description=description,
        )

    def get(self, policy_id: str) -> _FakePolicyRef:
        return _FakePolicyRef(self.policies[policy_id])


class _FakePlatformClient:
    def __init__(self) -> None:
        self.secrets = _FakeSecretsClient()
        self.network_policies = _FakeNetworkPoliciesClient()


class _FakeRunloopClient:
    def __init__(self) -> None:
        self.platform = _FakePlatformClient()


@pytest.mark.asyncio
async def test_query_runloop_secret_returns_non_sensitive_metadata() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()
    secret = client.platform.secrets.add("RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN")

    result = await module._query_runloop_secret(  # noqa: SLF001
        client,
        name=secret.name,
    )

    assert result.found is True
    assert result.id == secret.id
    assert "value" not in result.model_dump(mode="json")


@pytest.mark.asyncio
async def test_query_runloop_secret_reports_missing_before_create() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()

    result = await module._query_runloop_secret(  # noqa: SLF001
        client,
        name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
    )

    assert result.found is False
    assert result.id is None


@pytest.mark.asyncio
async def test_query_runloop_network_policy_reports_existing_resource() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()
    policy = client.platform.network_policies.add(
        "runloop-capabilities-example-policy",
        description="Persistent example policy.",
    )

    result = await module._query_runloop_network_policy(  # noqa: SLF001
        client,
        name=policy.name,
    )

    assert result.found is True
    assert result.id == policy.id
    assert result.description == "Persistent example policy."


@pytest.mark.asyncio
async def test_bootstrap_persistent_resources_reuses_existing_resources_without_cleanup() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()
    secret = client.platform.secrets.add("RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN")
    policy = client.platform.network_policies.add("runloop-capabilities-example-policy")
    query_results = {
        "secret": module.RunloopResourceQueryResult(
            resource_type="secret",
            name=secret.name,
            found=True,
            id=secret.id,
        ),
        "network_policy": module.RunloopResourceQueryResult(
            resource_type="network_policy",
            name=policy.name,
            found=True,
            id=policy.id,
        ),
    }

    bootstrap = await module._bootstrap_persistent_resources(  # noqa: SLF001
        client,
        managed_secret_name=secret.name,
        managed_secret_value="runloop-capabilities-example-token",
        network_policy_name=policy.name,
        network_policy_id_override=None,
        query_results=query_results,
        axon_name=None,
    )

    secret_bootstrap = bootstrap["secret"]
    network_policy_bootstrap = bootstrap["network_policy"]
    assert secret_bootstrap.action == "reused"
    assert network_policy_bootstrap.action == "reused"
    assert client.platform.secrets.create_calls == []
    assert client.platform.network_policies.create_calls == []
    assert client.platform.secrets.delete_calls == []
    assert client.platform.network_policies.delete_calls == []


@pytest.mark.asyncio
async def test_bootstrap_persistent_resources_creates_missing_resources() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()
    query_results = {
        "secret": module.RunloopResourceQueryResult(
            resource_type="secret",
            name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
            found=False,
        ),
        "network_policy": module.RunloopResourceQueryResult(
            resource_type="network_policy",
            name="runloop-capabilities-example-policy",
            found=False,
        ),
    }

    bootstrap = await module._bootstrap_persistent_resources(  # noqa: SLF001
        client,
        managed_secret_name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
        managed_secret_value="runloop-capabilities-example-token",
        network_policy_name="runloop-capabilities-example-policy",
        network_policy_id_override=None,
        query_results=query_results,
        axon_name=None,
    )

    secret_bootstrap = bootstrap["secret"]
    network_policy_bootstrap = bootstrap["network_policy"]
    assert secret_bootstrap.action == "created"
    assert network_policy_bootstrap.action == "created"
    assert client.platform.secrets.create_calls == [
        ("RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN", "runloop-capabilities-example-token")
    ]
    assert client.platform.network_policies.create_calls == [
        {
            "name": "runloop-capabilities-example-policy",
            "allow_all": True,
            "description": "Persistent network policy for the Runloop capabilities example.",
        }
    ]


@pytest.mark.asyncio
async def test_bootstrap_persistent_resources_respects_policy_override() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()
    query_results = {
        "secret": module.RunloopResourceQueryResult(
            resource_type="secret",
            name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
            found=False,
        ),
        "network_policy": module.RunloopResourceQueryResult(
            resource_type="network_policy",
            name="runloop-capabilities-example-policy",
            found=False,
        ),
    }

    bootstrap = await module._bootstrap_persistent_resources(  # noqa: SLF001
        client,
        managed_secret_name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
        managed_secret_value="runloop-capabilities-example-token",
        network_policy_name="runloop-capabilities-example-policy",
        network_policy_id_override="np-override",
        query_results=query_results,
        axon_name=None,
    )

    network_policy_bootstrap = bootstrap["network_policy"]
    assert network_policy_bootstrap.action == "override"
    assert network_policy_bootstrap.id == "np-override"
    assert client.platform.network_policies.create_calls == []


@pytest.mark.asyncio
async def test_bootstrap_persistent_resources_recovers_from_existing_policy_conflict() -> None:
    module = _load_example_module()
    client = _FakeRunloopClient()
    policy = client.platform.network_policies.add(
        "runloop-capabilities-example-policy",
        description="Persistent example policy.",
    )
    query_results = {
        "secret": module.RunloopResourceQueryResult(
            resource_type="secret",
            name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
            found=False,
        ),
        "network_policy": module.RunloopResourceQueryResult(
            resource_type="network_policy",
            name=policy.name,
            found=False,
        ),
    }

    bootstrap = await module._bootstrap_persistent_resources(  # noqa: SLF001
        client,
        managed_secret_name="RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN",
        managed_secret_value="runloop-capabilities-example-token",
        network_policy_name=policy.name,
        network_policy_id_override=None,
        query_results=query_results,
        axon_name=None,
    )

    network_policy_bootstrap = bootstrap["network_policy"]
    assert network_policy_bootstrap.action == "reused"
    assert network_policy_bootstrap.found_before_bootstrap is True
    assert network_policy_bootstrap.id == policy.id
