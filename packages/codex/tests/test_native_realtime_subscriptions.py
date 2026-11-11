"""Passive listeners share the native connection and isolate slow readers."""

import asyncio
from contextlib import aclosing

import pytest

from agents import Agent, NativeProvider
from agents.realtime import (
    RealtimeConfig,
    RealtimeLifecycle,
    RealtimePlaybackInterrupted,
    RealtimeText,
)
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


async def next_text(events):
    async for event in events:
        if isinstance(event, RealtimeText):
            return event.text
    pytest.fail("Realtime stream ended before text")


async def send_text(peer: RealtimePeer, text: str, *, response_id="response"):
    assert peer.socket is not None
    await peer.socket.send_json({"type": "response.created", "response": {"id": response_id}})
    await peer.socket.send_json(
        {
            "type": "response.output_text.delta",
            "response_id": response_id,
            "item_id": f"item-{response_id}",
            "content_index": 0,
            "output_index": 0,
            "delta": text,
        }
    )


async def test_native_subscribers_share_one_connection_and_close_independently(realtime_peer):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            async with (
                aclosing(voice.events()) as primary,
                aclosing(voice.subscribe()) as first,
                aclosing(voice.subscribe()) as second,
            ):
                await send_text(peer, "first")
                assert (
                    await asyncio.gather(next_text(primary), next_text(first), next_text(second))
                    == ["first"] * 3
                )
                await first.aclose()
                with pytest.raises(StopAsyncIteration):
                    await anext(first)
                await send_text(peer, "second")
                assert await asyncio.gather(next_text(primary), next_text(second)) == ["second"] * 2
                assert peer.connections == 1
                await voice.aclose()
                assert [event async for event in second] == [RealtimeLifecycle("closed")]


@pytest.mark.parametrize("capacity", [{"max_events": 1}, {"max_bytes": 1}])
async def test_slow_subscriber_capacity_does_not_fail_other_readers(realtime_peer, capacity):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            async with (
                aclosing(voice.events()) as primary,
                aclosing(voice.subscribe(**capacity)) as slow,
                aclosing(voice.subscribe()) as fast,
            ):
                for text in ("first", "second"):
                    await send_text(peer, text)
                    assert await asyncio.gather(next_text(primary), next_text(fast)) == [text] * 2
                with pytest.raises(RuntimeError, match="subscriber capacity"):
                    await anext(slow)
                await send_text(peer, "still connected")
                assert (
                    await asyncio.gather(next_text(primary), next_text(fast))
                    == ["still connected"] * 2
                )
                assert peer.connections == 1


async def test_cancelled_subscriber_releases_capacity_without_closing_native_owner(realtime_peer):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            reader = voice.subscribe()
            pending = asyncio.create_task(next_text(reader))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            remaining = [voice.subscribe() for _ in range(16)]
            try:
                with pytest.raises(RuntimeError, match="subscriber count"):
                    voice.subscribe()
                await send_text(peer, "alive")
                assert await next_text(remaining[0]) == "alive"
                assert peer.connections == 1
            finally:
                for subscriber in remaining:
                    await subscriber.aclose()


async def test_native_failure_reaches_primary_and_subscribers(realtime_peer):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            async with (
                aclosing(voice.events()) as primary,
                aclosing(voice.subscribe()) as subscriber,
            ):
                assert peer.socket is not None
                await peer.socket.close()
                with pytest.raises(RuntimeError) as observer_failure:
                    await next_text(subscriber)
                with pytest.raises(RuntimeError) as primary_failure:
                    await next_text(primary)
                assert observer_failure.value is primary_failure.value
                assert peer.disconnected.is_set()


async def test_interrupt_discards_retired_text_from_every_subscriber(realtime_peer):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            async with aclosing(voice.events()) as primary, aclosing(voice.subscribe()) as observer:
                await send_text(peer, "retired text")
                assert await next_text(primary) == "retired text"
                await voice.interrupt(response_id="response")
                received = []
                async for event in observer:
                    received.append(event)
                    if isinstance(event, RealtimePlaybackInterrupted):
                        break
                assert not any(isinstance(event, RealtimeText) for event in received)
                assert peer.socket is not None
                await peer.socket.send_json(
                    {"type": "response.done", "response": {"id": "response"}}
                )
                await send_text(peer, "after interrupt", response_id="next-response")
                assert await next_text(observer) == "after interrupt"


async def test_subscriber_only_pressure_sheds_bounded_primary_backlog(realtime_peer):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            async with aclosing(voice.subscribe()) as observer:
                for index in range(140):
                    text = str(index)
                    await send_text(peer, text)
                    assert await next_text(observer) == text
                assert voice._queue.qsize() <= voice._EVENT_LIMIT
                assert voice._queue_bytes <= voice._BUFFER_BYTES_LIMIT
                assert peer.connections == 1
                await voice.aclose()
                assert [event async for event in observer] == [RealtimeLifecycle("closed")]


async def test_acquired_primary_restores_strict_capacity_after_subscribing(realtime_peer):
    peer = realtime_peer
    agent = Agent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=peer.responses.url, api_key="synthetic-key"),
    )
    async with timeout(10), agent.session() as session:
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            async with aclosing(voice.events()) as primary, aclosing(voice.subscribe()) as observer:
                await send_text(peer, "acquire")
                assert await next_text(primary) == "acquire"
                assert await next_text(observer) == "acquire"
                # Closing the primary cursor does not waive its acquired policy.
                await primary.aclose()
                with pytest.raises(RuntimeError, match="buffer overflow"):
                    for index in range(140):
                        text = str(index)
                        await send_text(peer, text)
                        assert await next_text(observer) == text


def test_native_voice_reexports_existing_stt_runtime_types():
    from agents.classic.voice import (
        OpenAISTTTranscriptionSession as ExistingSession,
        STTWebsocketConnectionError as ExistingError,
    )
    from agents.voice import OpenAISTTTranscriptionSession, STTWebsocketConnectionError

    assert OpenAISTTTranscriptionSession is ExistingSession
    assert STTWebsocketConnectionError is ExistingError


async def test_subscriber_terminal_cleanup_preserves_primary_failure():
    from types import SimpleNamespace

    from _agents_codex.realtime import _ObservationBuffer

    primary = RuntimeError("provider connection failed")
    cleanup = RuntimeError("owned media cleanup failed")
    terminal = asyncio.get_running_loop().create_future()
    terminal.set_result(primary)
    close_calls = []

    async def close():
        close_calls.append(True)
        raise cleanup

    owner = SimpleNamespace(_terminal=terminal, _subscribers=set(), aclose=close)
    subscriber = _ObservationBuffer(owner, max_events=1, max_bytes=1024)
    owner._subscribers.add(subscriber)
    subscriber.finish(primary)
    with pytest.raises(RuntimeError) as failure:
        await anext(subscriber)
    assert failure.value is primary
    assert primary.__context__ is cleanup
    assert close_calls == [True]


async def test_raw_provider_observations_are_detached_at_each_delivery():
    from types import SimpleNamespace

    from _agents_codex.realtime import RealtimeRawProviderEvent, RealtimeSession

    owner = RealtimeSession(SimpleNamespace(_opening_agent=None), config=RealtimeConfig())
    owner._live = True
    owner._ready.set_result(None)
    primary = owner.events()
    first, second = owner.subscribe(), owner.subscribe()
    source = {"type": "fixture.event", "payload": {"values": [{"text": "original"}]}}
    owner._emit(RealtimeRawProviderEvent(source))
    try:
        delivered = await anext(primary)
        delivered.event["payload"]["values"][0]["text"] = "changed by primary"
        observed = await anext(first)
        assert observed.event == source
        assert observed.event["payload"]["values"][0]["text"] == "original"
        observed.event["payload"]["values"].append({"text": "changed by first subscriber"})
        remaining = await anext(second)
        assert remaining.event["payload"]["values"] == [{"text": "original"}]
        assert source["payload"]["values"] == [{"text": "original"}]
    finally:
        await primary.aclose()
        await first.aclose()
        await second.aclose()


async def test_hosted_tool_schemas_are_detached_without_cloning_issued_approvals():
    from types import SimpleNamespace

    from _agents_codex.realtime import (
        RealtimeHostedMcpApprovalRequested,
        RealtimeHostedMcpTool,
        RealtimeHostedMcpToolsListed,
        RealtimeSession,
    )

    owner = RealtimeSession(SimpleNamespace(_opening_agent=None), config=RealtimeConfig())
    owner._live = True
    owner._ready.set_result(None)
    primary = owner.events()
    first, second = owner.subscribe(), owner.subscribe()
    schema = {"type": "object", "properties": {"record": {"enum": ["original"]}}}
    owner._emit(
        RealtimeHostedMcpToolsListed(
            "catalog", "records", (RealtimeHostedMcpTool("lookup", schema),)
        )
    )
    try:
        delivered = await anext(primary)
        delivered.tools[0].input_schema["properties"]["record"]["enum"].append("primary")
        observed = await anext(first)
        assert observed.tools[0].input_schema == schema
        observed.tools[0].input_schema["properties"]["record"]["enum"].append("first")
        remaining = await anext(second)
        assert remaining.tools[0].input_schema["properties"]["record"]["enum"] == ["original"]
        assert schema["properties"]["record"]["enum"] == ["original"]

        approval = RealtimeHostedMcpApprovalRequested(
            "request", "records", "lookup", "{}", "response"
        )
        owner._emit(approval)
        assert await anext(primary) is approval
        assert await anext(first) is approval
        assert await anext(second) is approval
    finally:
        await primary.aclose()
        await first.aclose()
        await second.aclose()
