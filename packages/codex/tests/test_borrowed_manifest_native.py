import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import Agent, Environment
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, sandbox_environment
from tests.native_fixtures import responses as responses
from tests.test_native_agent import provider

if sys.platform in ("linux", "darwin"):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="UnixLocal resource"
)


async def test_native_has_been_rejecting_nonatomic_borrowed_addition_before_gateway(responses):
    from agents.classic.tracing import set_tracing_disabled

    set_tracing_disabled(True)
    client = UnixLocalSandboxClient()
    selected = await client.create(manifest=Manifest(entries={"old.txt": File(content=b"old")}))
    await selected.start()
    root = Path(selected.state.manifest.root)
    original = selected.state.manifest
    target = original.model_copy(
        update={"entries": {**original.entries, "added.txt": File(content=b"addition")}}, deep=True
    )
    connected = False

    @asynccontextmanager
    async def connect(sandbox):
        nonlocal connected
        connected = True
        yield Environment(url="wss://executor.invalid", cwd=str(root))

    try:
        source = sandbox_environment(
            client,
            options=UnixLocalSandboxClientOptions(),
            manifest=target,
            session=selected,
            connect=connect,
        )
        with pytest.raises(ValueError, match="atomic provider transaction"):
            async with Agent(
                instructions="Use the workspace.", provider=provider(responses)
            ).session(environment=source):
                raise AssertionError("Unexpected session")
        assert not connected
        assert selected.state.manifest is original
        assert not (root / "added.txt").exists()
        assert (root / "old.txt").read_bytes() == b"old"
        assert not responses.requests
    finally:
        await selected.aclose()


async def test_native_has_been_rejecting_entry_removal_before_gateway(responses):
    from agents.classic.tracing import set_tracing_disabled

    set_tracing_disabled(True)
    client = UnixLocalSandboxClient()
    selected = await client.create(manifest=Manifest(entries={"old.txt": File(content=b"old")}))
    await selected.start()
    root = Path(selected.state.manifest.root)
    target = selected.state.manifest.model_copy(update={"entries": {}}, deep=True)
    connected = False

    @asynccontextmanager
    async def connect(sandbox):
        nonlocal connected
        connected = True
        yield Environment(url="wss://executor.invalid", cwd=str(root))

    try:
        source = sandbox_environment(
            client,
            options=UnixLocalSandboxClientOptions(),
            manifest=target,
            session=selected,
            connect=connect,
        )
        with pytest.raises(ValueError, match="removing manifest entries"):
            async with Agent(
                instructions="Use the workspace.", provider=provider(responses)
            ).session(environment=source):
                raise AssertionError("Unexpected session")
        assert not connected
        assert (root / "old.txt").read_bytes() == b"old"
    finally:
        await selected.aclose()


async def test_native_has_been_preserving_borrowed_manifest_before_rejected_batch(
    monkeypatch, responses
):
    from agents.classic.tracing import set_tracing_disabled

    set_tracing_disabled(True)
    client = UnixLocalSandboxClient()
    selected = await client.create(manifest=Manifest(entries={"old.txt": File(content=b"old")}))
    await selected.start()
    root = Path(selected.state.manifest.root)
    original = selected.state.manifest
    target = original.model_copy(
        update={"entries": {**original.entries, "added.txt": File(content=b"addition")}}, deep=True
    )
    connected = False

    applied = False

    async def fail(*args, **kwargs):
        nonlocal applied
        applied = True
        raise RuntimeError("Provider materialization failed")

    monkeypatch.setattr(selected, "_apply_entry_batch", fail)

    @asynccontextmanager
    async def connect(sandbox):
        nonlocal connected
        connected = True
        yield Environment(url="wss://executor.invalid", cwd=str(root))

    try:
        source = sandbox_environment(
            client,
            options=UnixLocalSandboxClientOptions(),
            manifest=target,
            session=selected,
            connect=connect,
        )
        with pytest.raises(ValueError, match="atomic provider transaction"):
            async with Agent(
                instructions="Use the workspace.", provider=provider(responses)
            ).session(environment=source):
                raise AssertionError("Unexpected session")
        assert not connected
        assert not applied
        assert selected.state.manifest is original
        assert not (root / "added.txt").exists()
        assert (root / "old.txt").read_bytes() == b"old"
        assert not responses.requests
    finally:
        await selected.aclose()
