"""Per-connection credentials through the public API and native provider wire."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("source_kind", ["static", "sync", "async"])
async def test_credentials_resolve_per_connection_without_replacing_session_auth(
    realtime_peer: RealtimePeer, source_kind: str
) -> None:
    peer = realtime_peer
    resolutions = 0

    def resolve() -> str:
        nonlocal resolutions
        resolutions += 1
        return f"voice-only-key-{resolutions}"

    async def resolve_async() -> str:
        return resolve()

    source = (
        "voice-static-key"
        if source_kind == "static"
        else (resolve if source_kind == "sync" else resolve_async)
    )
    config = RealtimeConfig(model="fixture-realtime", api_key=source)
    assert "voice-static-key" not in repr(config)
    peer.responses.respond(*text_response("ordinary-auth", "Ordinary auth survived."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="ordinary-session-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        for _ in range(2):
            async with session.realtime(config=config) as voice:
                await voice.send_text("Use this connection's credentials.")
        assert [headers["authorization"] for headers in peer.upgrades] == (
            ["Bearer voice-static-key"] * 2
            if source_kind == "static"
            else ["Bearer voice-only-key-1", "Bearer voice-only-key-2"]
        )
        assert resolutions == (0 if source_kind == "static" else 2)
        turn = await session.submit("Use the original provider.")
        assert (await turn.get_result()).final_output == "Ordinary auth survived."
        assert peer.responses.request_headers[0]["authorization"] == "Bearer ordinary-session-key"


@pytest.mark.parametrize("return_after_cancel", [False, True])
async def test_close_cancels_credentials_and_rejects_a_late_key(
    realtime_peer: RealtimePeer, return_after_cancel: bool
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()

    async def resolve() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not return_after_cancel:
                raise
            await release.wait()
            return "late-key-must-never-connect"
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="ordinary-session-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        voice = session.realtime(config=RealtimeConfig(model="fixture-realtime", api_key=resolve))
        opening = asyncio.create_task(voice.__aenter__())
        await entered.wait()
        closing = asyncio.create_task(voice.aclose())
        try:
            await cancelled.wait()
            release.set()
            async with timeout(5):
                await closing
                with pytest.raises(RuntimeError if return_after_cancel else asyncio.CancelledError):
                    await opening
            assert stopped.is_set()
            assert peer.connections == 0
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                assert peer.connections == 1
        finally:
            release.set()
            await asyncio.gather(opening, closing, return_exceptions=True)


@pytest.mark.parametrize("invalid_key", ["", None])
async def test_invalid_resolved_credentials_release_prepared_generation(
    realtime_peer: RealtimePeer, invalid_key: Any
) -> None:
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="ordinary-session-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        with pytest.raises(ValueError, match="credential source must return a nonempty string"):
            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime", api_key=lambda: invalid_key)
            ):
                raise AssertionError("invalid credentials became ready")
        assert peer.connections == 0
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
            assert peer.connections == 1


async def test_credential_callback_failure_stays_local(realtime_peer: RealtimePeer) -> None:
    peer = realtime_peer
    problem = LookupError("private credential resolver failure")

    async def resolve() -> str:
        raise problem

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="ordinary-session-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        with pytest.raises(LookupError) as failed:
            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime", api_key=resolve)
            ):
                raise AssertionError("failed credentials became ready")
        assert failed.value is problem
        assert peer.connections == 0
        assert not peer.responses.requests
