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
from tests._asyncio import capture_cancellation
from tests.test_sandbox_environment import Client, Options


def _native_client(process: SimpleNamespace, *, cleanup_s: int = 30) -> Client:
    class NativeClient(Client):
        backend_id = "daytona"

        async def create(self, *, manifest=None, options, snapshot=None) -> SandboxSession:
            assert manifest is not None
            self._validate_manifest_for_create(manifest)
            state = daytona.DaytonaSandboxSessionState(
                sandbox_id="native-fixture",
                manifest=manifest,
                snapshot=NoopSnapshot(id="native-fixture"),
                timeouts=daytona.DaytonaSandboxTimeouts(cleanup_s=cleanup_s),
            )
            inner = daytona.DaytonaSandboxSession(
                state=state,
                sandbox=SimpleNamespace(process=process, delete=AsyncMock()),
            )
            self.session = self._wrap_session(inner)
            self.session.start = AsyncMock()
            return self.session

    return NativeClient([])


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

    client = _native_client(process)

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


@pytest.mark.parametrize("operation", ["close", "terminate"])
@pytest.mark.parametrize("entry_count", [1, 2])
async def test_native_daytona_cleanup_settles_after_cancellation(
    monkeypatch: pytest.MonkeyPatch, operation: str, entry_count: int
) -> None:
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    closed: list[int] = []

    def make_handle(index: int) -> SimpleNamespace:
        waiter_exited = asyncio.Event()

        async def wait() -> None:
            try:
                await asyncio.Future()
            finally:
                waiter_exited.set()

        async def disconnect() -> None:
            assert waiter_exited.is_set()
            if index == 0:
                disconnect_started.set()
                await release_disconnect.wait()
            closed.append(index)

        return SimpleNamespace(
            wait_for_connection=AsyncMock(),
            send_input=AsyncMock(),
            wait=wait,
            disconnect=AsyncMock(side_effect=disconnect),
        )

    handles = [make_handle(index) for index in range(entry_count)]
    process = SimpleNamespace(
        create_pty_session=AsyncMock(side_effect=handles),
        kill_pty_session=AsyncMock(),
    )
    monkeypatch.setattr(daytona, "_import_pty_size", lambda: SimpleNamespace)
    monkeypatch.setattr(daytona, "_daytona_timeout_error_types", lambda: ())
    client = _native_client(process)

    @asynccontextmanager
    async def connect(resource: SandboxSession) -> AsyncIterator[Environment[None]]:
        for _ in range(entry_count):
            await resource.pty_exec_start("sleep", "60", tty=True, yield_time_s=0.001)
        yield Environment(url="wss://fixture.invalid", cwd="/workspace")

    async with sandbox_environment(client, manifest=Manifest(), options=Options(), connect=connect):
        assert client.session is not None
        close = client.session.aclose if operation == "close" else client.session.pty_terminate_all
        cancellations: list[asyncio.CancelledError] = []
        task = asyncio.create_task(capture_cancellation(close(), cancellations))
        await asyncio.wait_for(disconnect_started.wait(), timeout=5)
        task.cancel("first cancellation")
        await asyncio.sleep(0)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        release_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert [error.args for error in cancellations] == [("first cancellation",)]
        assert closed == list(range(entry_count))
        await client.session.pty_terminate_all()
        for handle in handles:
            handle.disconnect.assert_awaited_once()


async def test_native_daytona_completed_pty_preserves_first_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_started = asyncio.Event()
    release_kill = asyncio.Event()
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    closed = asyncio.Event()

    async def kill(_session_id: str) -> None:
        kill_started.set()
        await release_kill.wait()

    async def disconnect() -> None:
        disconnect_started.set()
        await release_disconnect.wait()
        closed.set()

    handle = SimpleNamespace(
        wait_for_connection=AsyncMock(),
        send_input=AsyncMock(),
        wait=AsyncMock(),
        exit_code=0,
        disconnect=AsyncMock(side_effect=disconnect),
    )
    process = SimpleNamespace(
        create_pty_session=AsyncMock(return_value=handle),
        kill_pty_session=AsyncMock(side_effect=kill),
    )
    monkeypatch.setattr(daytona, "_import_pty_size", lambda: SimpleNamespace)
    monkeypatch.setattr(daytona, "_daytona_timeout_error_types", lambda: ())
    client = _native_client(process)

    @asynccontextmanager
    async def connect(resource: SandboxSession) -> AsyncIterator[Environment[None]]:
        yield Environment(url="wss://fixture.invalid", cwd="/workspace")

    async with sandbox_environment(client, manifest=Manifest(), options=Options(), connect=connect):
        assert client.session is not None
        cancellations: list[asyncio.CancelledError] = []
        task = asyncio.create_task(
            capture_cancellation(
                client.session.pty_exec_start("true", tty=True, yield_time_s=0.001), cancellations
            )
        )
        try:
            await asyncio.wait_for(kill_started.wait(), timeout=5)
            task.cancel("first cancellation")
            release_kill.set()
            await asyncio.wait_for(disconnect_started.wait(), timeout=5)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            release_disconnect.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert [error.args for error in cancellations] == [("first cancellation",)]
            assert closed.is_set()
            handle.disconnect.assert_awaited_once()
        finally:
            release_kill.set()
            release_disconnect.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_native_daytona_cancelled_cleanup_bounds_provider_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_started = asyncio.Event()
    release_kill = asyncio.Event()

    async def kill(_session_id: str) -> None:
        kill_started.set()
        await release_kill.wait()

    async def wait() -> None:
        await asyncio.Future()

    handle = SimpleNamespace(
        wait_for_connection=AsyncMock(),
        send_input=AsyncMock(),
        wait=wait,
        disconnect=AsyncMock(),
    )
    process = SimpleNamespace(
        create_pty_session=AsyncMock(return_value=handle),
        kill_pty_session=AsyncMock(side_effect=kill),
    )
    monkeypatch.setattr(daytona, "_import_pty_size", lambda: SimpleNamespace)
    monkeypatch.setattr(daytona, "_daytona_timeout_error_types", lambda: ())
    client = _native_client(process, cleanup_s=1)

    @asynccontextmanager
    async def connect(resource: SandboxSession) -> AsyncIterator[Environment[None]]:
        await resource.pty_exec_start("sleep", "60", tty=True, yield_time_s=0.001)
        yield Environment(url="wss://fixture.invalid", cwd="/workspace")

    async with sandbox_environment(client, manifest=Manifest(), options=Options(), connect=connect):
        assert client.session is not None
        cancellations: list[asyncio.CancelledError] = []
        task = asyncio.create_task(capture_cancellation(client.session.aclose(), cancellations))
        try:
            await asyncio.wait_for(kill_started.wait(), timeout=5)
            task.cancel("first cancellation")
            done, _ = await asyncio.wait({task}, timeout=3)
            assert task in done, "provider kill exceeded the cleanup timeout"
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert [error.args for error in cancellations] == [("first cancellation",)]
            handle.disconnect.assert_awaited_once()
        finally:
            release_kill.set()
            await asyncio.gather(task, return_exceptions=True)
