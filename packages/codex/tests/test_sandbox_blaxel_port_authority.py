"""Current native acquisition policy precedes Blaxel resume and preview effects."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import Environment
from agents.classic.extensions.sandbox.blaxel import sandbox as provider
from agents.classic.testing.sandbox import scripted_sandbox_session
from agents.sandbox import Manifest, SandboxSession, sandbox_environment


@pytest.mark.parametrize("lifecycle", ["borrowed", "resume"])
@pytest.mark.parametrize(
    "saved_public,current_public", [(True, False), (True, True), (False, False), (False, True)]
)
async def test_current_blaxel_visibility_precedes_resume_and_preview(
    monkeypatch, lifecycle, saved_public, current_public
):
    monkeypatch.setattr(provider, "_import_blaxel_sdk", lambda: object())
    manifest = Manifest(root="/trusted")
    snapshot = scripted_sandbox_session(manifest=manifest).state.snapshot
    state = provider.BlaxelSandboxSessionState(
        snapshot=snapshot,
        manifest=manifest,
        sandbox_name="synthetic",
        exposed_port_public=saved_public,
    )
    preview = SimpleNamespace(
        spec=SimpleNamespace(url="https://preview.fixture.invalid"),
        tokens=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(value="synthetic"))),
    )
    create_preview = AsyncMock(return_value=preview)
    inner = provider.BlaxelSandboxSession.from_state(
        state,
        sandbox=SimpleNamespace(previews=SimpleNamespace(create_if_not_exists=create_preview)),
    )
    live = SandboxSession(inner)
    client = provider.BlaxelSandboxClient()
    options = provider.BlaxelSandboxClientOptions(exposed_port_public=current_public)
    resume = AsyncMock(return_value=live)
    monkeypatch.setattr(client, "resume", resume)
    monkeypatch.setattr(client, "delete", AsyncMock())
    monkeypatch.setattr(live, "start", AsyncMock())
    monkeypatch.setattr(live, "aclose", AsyncMock())
    connected = []

    @asynccontextmanager
    async def connect(selected):
        connected.append(selected)
        await selected.resolve_exposed_port(3000)
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

    if saved_public and not current_public:
        denied = False
        try:
            await acquire()
        except ValueError as error:
            assert "port exposure" in str(error)
            denied = True
        assert create_preview.await_args_list == [], (
            "Current private policy reached provider preview creation"
        )
        assert connected == []
        resume.assert_not_awaited()
        client.delete.assert_not_awaited()
        live.start.assert_not_awaited()
        live.aclose.assert_not_awaited()
        assert denied
    else:
        await acquire()
        assert connected == [live]
        assert resume.await_count == (lifecycle == "resume")
        assert create_preview.await_args.args[0]["spec"]["public"] is saved_public
