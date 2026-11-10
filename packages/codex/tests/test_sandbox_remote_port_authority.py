"""Native acquisitions reject stale provider port authority before effects."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from agents import Environment
from agents.classic.testing.sandbox import scripted_sandbox_session
from agents.sandbox import Manifest, SandboxSession, sandbox_environment


@pytest.mark.parametrize("backend", ["daytona", "modal"])
@pytest.mark.parametrize("lifecycle", ["borrowed", "resume"])
@pytest.mark.parametrize("change", ["matching", "revoked", "changed"])
async def test_current_remote_ports_precede_provider_and_gateway(
    monkeypatch, backend, lifecycle, change
):
    manifest = Manifest(root="/trusted")
    scripted = scripted_sandbox_session(manifest=manifest)
    recorded = (3000, 8080) if change == "matching" else (3000,)
    requested = (8080, 3000) if change == "matching" else () if change == "revoked" else (4000,)
    if backend == "daytona":
        from agents.classic.extensions.sandbox.daytona import sandbox as provider

        monkeypatch.setattr(
            provider,
            "_import_daytona_sdk",
            lambda: (lambda config: object(), object, object, object),
        )
        client = provider.DaytonaSandboxClient()
        options = provider.DaytonaSandboxClientOptions(exposed_ports=requested)
        state = provider.DaytonaSandboxSessionState(
            snapshot=scripted.state.snapshot,
            manifest=manifest,
            sandbox_id="synthetic",
            exposed_ports=recorded,
        )
    else:
        from agents.classic.extensions.sandbox.modal import sandbox as provider

        client = provider.ModalSandboxClient()
        options = provider.ModalSandboxClientOptions(app_name="synthetic", exposed_ports=requested)
        state = provider.ModalSandboxSessionState(
            snapshot=scripted.state.snapshot,
            manifest=manifest,
            app_name="synthetic",
            sandbox_id="synthetic",
            exposed_ports=recorded,
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
        yield Environment(url="wss://fixture.invalid", cwd="/trusted")

    async def acquire():
        source = sandbox_environment(
            client,
            manifest=manifest,
            options=options,
            connect=connect,
            **(
                {"session": live} if lifecycle == "borrowed" else {"authorized_resume_state": state}
            ),
        )
        async with source:
            pass

    if change == "matching":
        await acquire()
        assert connected == [live]
        assert resume.await_count == (lifecycle == "resume")
    else:
        with pytest.raises(ValueError, match="port exposure"):
            await acquire()
        assert connected == []
        resume.assert_not_awaited()
        client.delete.assert_not_awaited()
        live.start.assert_not_awaited()
        live.aclose.assert_not_awaited()
