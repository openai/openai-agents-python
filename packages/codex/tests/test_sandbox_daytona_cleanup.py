"""Owned native sandbox sources close the provider's PTY connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import Environment
from agents.classic.extensions.sandbox.daytona import sandbox as daytona
from agents.sandbox import Manifest, NoopSnapshot, SandboxSession, sandbox_environment
from tests.test_sandbox_environment import Client, Options


@pytest.mark.parametrize(
    ("kill_fails", "disconnect_fails", "body_fails"),
    [(False, False, False), (True, False, True), (True, True, True)],
)
async def test_native_source_closes_daytona_pty(
    monkeypatch: pytest.MonkeyPatch,
    kill_fails: bool,
    disconnect_fails: bool,
    body_fails: bool,
) -> None:
    waiter_exited = asyncio.Event()

    async def wait() -> None:
        try:
            await asyncio.Future()
        finally:
            waiter_exited.set()

    async def disconnect() -> None:
        assert waiter_exited.is_set()
        if disconnect_fails:
            raise RuntimeError("disconnect failed")

    handle = SimpleNamespace(
        wait_for_connection=AsyncMock(),
        send_input=AsyncMock(),
        wait=wait,
        disconnect=AsyncMock(side_effect=disconnect),
    )
    process = SimpleNamespace(
        create_pty_session=AsyncMock(return_value=handle),
        kill_pty_session=AsyncMock(side_effect=RuntimeError("kill failed") if kill_fails else None),
    )
    monkeypatch.setattr(daytona, "_import_pty_size", lambda: SimpleNamespace)
    monkeypatch.setattr(daytona, "_daytona_timeout_error_types", lambda: ())

    class NativeClient(Client):
        backend_id = "daytona"

        async def create(self, *, manifest=None, options, snapshot=None) -> SandboxSession:
            assert manifest is not None
            self._validate_manifest_for_create(manifest)
            state = daytona.DaytonaSandboxSessionState(
                sandbox_id="native-fixture",
                manifest=manifest,
                snapshot=NoopSnapshot(id="native-fixture"),
            )
            inner = daytona.DaytonaSandboxSession(
                state=state,
                sandbox=SimpleNamespace(process=process, delete=AsyncMock()),
            )
            self.session = self._wrap_session(inner)
            self.session.start = AsyncMock()
            return self.session

    client = NativeClient([])

    @asynccontextmanager
    async def connect(resource: SandboxSession) -> AsyncIterator[Environment[None]]:
        await resource.pty_exec_start("sleep", "60", tty=True, yield_time_s=0.001)
        yield Environment(url="wss://fixture.invalid", cwd="/workspace")

    primary = RuntimeError("body failed")
    try:
        async with sandbox_environment(
            client, manifest=Manifest(), options=Options(), connect=connect
        ):
            if body_fails:
                raise primary
    except RuntimeError as error:
        assert body_fails and error is primary
    else:
        assert not body_fails

    handle.disconnect.assert_awaited_once()
    process.kill_pty_session.assert_awaited_once()
    assert waiter_exited.is_set()
    assert client.session is not None
    await client.session.pty_terminate_all()
    handle.disconnect.assert_awaited_once()
