"""The selected native environment uses only the current Cloudflare worker."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from agents import Environment
from agents.classic.extensions.sandbox.cloudflare.sandbox import (
    CloudflareSandboxClient,
    CloudflareSandboxClientOptions,
    CloudflareSandboxSessionState,
)
from agents.classic.testing.sandbox import scripted_sandbox_session
from agents.sandbox import Manifest, SandboxSession, sandbox_environment


@pytest.mark.parametrize("lifecycle", ["borrowed", "resume"])
@pytest.mark.parametrize("changed", ["none", "worker", "ports"])
async def test_current_cloudflare_worker_and_ports_precede_native_connection(
    monkeypatch, lifecycle, changed
):
    manifest = Manifest(root="/workspace")
    scripted = scripted_sandbox_session(manifest=manifest)
    state = CloudflareSandboxSessionState(
        snapshot=scripted.state.snapshot,
        manifest=manifest,
        worker_url="https://current.fixture.invalid/worker",
        sandbox_id="synthetic",
        exposed_ports=(3000, 8080),
    )
    client = CloudflareSandboxClient()
    options = CloudflareSandboxClientOptions(
        worker_url=(
            "https://changed.fixture.invalid/worker"
            if changed == "worker"
            else state.worker_url + "/"
        ),
        exposed_ports=() if changed == "ports" else (8080, 3000),
    )
    live = SandboxSession(scripted)
    live.state = state
    resume = AsyncMock(return_value=live)
    monkeypatch.setattr(client, "resume", resume)
    monkeypatch.setattr(client, "delete", AsyncMock())
    monkeypatch.setattr(live, "start", AsyncMock())
    monkeypatch.setattr(live, "aclose", AsyncMock())
    connected = []

    @asynccontextmanager
    async def connect(selected):
        connected.append(selected)
        yield Environment(url="wss://fixture.invalid", cwd="/workspace")

    async def acquire():
        authority = (
            {"session": live} if lifecycle == "borrowed" else {"authorized_resume_state": state}
        )
        source = sandbox_environment(
            client, manifest=manifest, options=options, connect=connect, **authority
        )
        async with source:
            pass

    if changed == "none":
        await acquire()
        assert connected == [live]
        assert resume.await_count == (lifecycle == "resume")
    else:
        with pytest.raises(ValueError, match="Cloudflare worker or port exposure"):
            await acquire()
        assert not connected
        resume.assert_not_awaited()
        client.delete.assert_not_awaited()
        live.start.assert_not_awaited()
        live.aclose.assert_not_awaited()
