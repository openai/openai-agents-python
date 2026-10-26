"""Check the external package example against the SDK without a Docker daemon."""

import importlib
from pathlib import Path
from types import ModuleType
from unittest.mock import create_autospec

import pytest
from pydantic import ValidationError

from agents.sandbox import Manifest
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
    DockerSandboxSessionState,
)
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSession
from agents.sandbox.snapshot import NoopSnapshot


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    source = Path(__file__).resolve().parents[2] / "examples/sandbox/external_provider/src"
    monkeypatch.syspath_prepend(str(source))
    return importlib.import_module("example_docker_provider")


def test_options_register_on_import(provider: ModuleType) -> None:
    options = BaseSandboxClientOptions.parse(
        {"type": "example_docker", "image": "python:3.14-slim"}
    )
    assert isinstance(options, provider.ExampleDockerOptions)
    assert options.model_dump() == {"type": "example_docker", "image": "python:3.14-slim"}
    with pytest.raises(ValidationError):
        provider.ExampleDockerOptions(image="")


@pytest.mark.asyncio
async def test_create_preserves_inputs_and_instrumented_session(provider: ModuleType) -> None:
    delegate = create_autospec(DockerSandboxClient, instance=True)
    session = create_autospec(SandboxSession, instance=True)
    delegate.create.return_value = session
    client = provider.ExampleDockerClient(delegate)
    manifest = Manifest()
    snapshot = NoopSnapshot(id="example-snapshot")

    result = await client.create(
        manifest=manifest,
        snapshot=snapshot,
        options=provider.ExampleDockerOptions(image="python:3.14-slim"),
    )

    assert result is session
    delegate.create.assert_awaited_once_with(
        manifest=manifest,
        snapshot=snapshot,
        options=DockerSandboxClientOptions(image="python:3.14-slim"),
    )


@pytest.mark.asyncio
async def test_resume_and_delete_preserve_delegate_ownership(provider: ModuleType) -> None:
    delegate = create_autospec(DockerSandboxClient, instance=True)
    session = create_autospec(SandboxSession, instance=True)
    delegate.resume.return_value = session
    delegate.delete.return_value = session
    client = provider.ExampleDockerClient(delegate)
    state = DockerSandboxSessionState(
        image="python:3.14-slim",
        container_id="example-container",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="example-snapshot"),
    )

    assert await client.resume(state) is session
    assert await client.delete(session) is session
    delegate.resume.assert_awaited_once_with(state)
    delegate.delete.assert_awaited_once_with(session)
    assert client.backend_id == DockerSandboxClient.backend_id == "docker"


def test_docker_state_round_trip_uses_real_delegate(provider: ModuleType) -> None:
    # Serialization needs no Docker connection; no Docker transport method is invoked.
    from docker import DockerClient

    connection = create_autospec(DockerClient, instance=True)
    delegate = DockerSandboxClient(connection)
    client = provider.ExampleDockerClient(delegate)
    state = DockerSandboxSessionState(
        image="python:3.14-slim",
        container_id="example-container",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="example-snapshot"),
    )

    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)

    assert payload["type"] == "docker"
    assert isinstance(restored, DockerSandboxSessionState)
    assert restored.container_id == state.container_id
    assert restored.session_id == state.session_id
    assert restored.image == state.image
