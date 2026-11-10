"""Joined native capability contracts.

Only the provider protocol is scripted. Agents, handoffs, context projection,
audio framing, interruption and function dispatch use the real native runtime.
"""

from __future__ import annotations

import base64
import json
from contextlib import aclosing
from copy import deepcopy
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, ToolContext, function_tool
from agents.model_context import ModelRequestContext

from ._asyncio import timeout
from .native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from .test_native_agent import function_declarations, provider


@pytest.mark.parametrize("selected", ["B", "C"])
async def test_native_selectable_handoffs_filter_destination_input_and_preserve_history(
    responses: ResponsesFixture, selected: str
) -> None:
    secret = "PRIVATE-SOURCE-HISTORY-MARKER"
    application = {"tenant": "synthetic-owner"}
    calls: list[tuple[str, str]] = []
    projections: list[tuple[str, ModelRequestContext[dict[str, str]]]] = []

    @function_tool
    async def remember(ctx: ToolContext[dict[str, str]], key: str) -> str:
        """Read a source-owned record."""
        assert ctx.context is application
        calls.append(("source", key))
        return "source-receipt"

    @function_tool(name="lookup")
    async def source_lookup(key: str) -> str:
        """This source callback must not run after transfer."""
        pytest.fail(f"Source lookup ran after transfer: {key}")

    def destination(label: str) -> Agent[dict[str, str], str]:
        @function_tool(name="lookup")
        async def lookup(ctx: ToolContext[dict[str, str]], key: str) -> str:
            """Read the active destination's record."""
            assert ctx.context is application
            calls.append((label, key))
            return f"{label}-receipt"

        async def filter_history(request: ModelRequestContext[dict[str, str]]):
            assert request.context is application
            projections.append((label, request))
            # Remove the source's private message without breaking retained
            # tool-call/output pairs. Canonical Session history stays intact.
            return [item for item in request.items if secret not in json.dumps(item)]

        return Agent(
            name=label,
            instructions=f"Destination {label} owns subsequent conversation control.",
            tools=[lookup],
            transform_context=filter_history,
        )

    left, right = destination("B"), destination("C")
    source = Agent(
        name="A",
        instructions="Read the source record, select a destination, and transfer control.",
        tools=[remember, source_lookup],
        handoffs=[left, right],
        provider=provider(responses),
    )
    responses.respond(function_call("remember", {"key": "source"}, "source-read"))
    responses.respond(function_call(f"transfer_to_{selected.lower()}", {}, "transfer"))
    responses.respond(function_call("lookup", {"key": "destination"}, "destination-read"))
    responses.respond(assistant_output(f"{selected} accepted"))

    async with timeout(30), source.session(context=application) as session:
        turn = await session.submit(f"Read, route, and keep this source-only fact: {secret}")
        assert (await turn.get_result()).final_output == f"{selected} accepted"
        assert await session.get_active_agent() is (left if selected == "B" else right)
        assert calls == [("source", "source"), (selected, "destination")]
        assert [label for label, _ in projections] == [selected, selected]
        assert all(request.session_id == session.id for _, request in projections)
        assert all(request.turn_id == turn.turn_id for _, request in projections)
        canonical = [item async for item in session.items()]
        assert secret in json.dumps(canonical)
        assert secret in (await session.save_state()).to_json()
        # The projection retains original items; filtering a request never
        # removes canonical source or destination function-call/output records.
        for call_id in ("source-read", "transfer", "destination-read"):
            assert any(
                item["type"] == "function_call" and item["call_id"] == call_id for item in canonical
            )
            assert any(
                item["type"] == "function_call_output" and item["call_id"] == call_id
                for item in canonical
            )

    assert len(responses.requests) == 4
    offered = {tool["name"] for tool in function_declarations(responses.requests[0])}
    assert {"transfer_to_b", "transfer_to_c"} <= offered
    assert secret in json.dumps(responses.requests[0])
    assert all(secret not in json.dumps(request) for request in responses.requests[2:])
    assert {tool["name"] for tool in function_declarations(responses.requests[2])} == {"lookup"}
    # HandoffExecutor returns this accepted native tool output, correlated with
    # the same provider call ID as the transfer request; filtering retains both.
    assert tool_output(responses.requests[2], "transfer") == "Handoff reserved."
    transfer_calls = [
        item
        for item in responses.requests[2]["input"]
        if item.get("type") == "function_call" and item.get("call_id") == "transfer"
    ]
    assert len(transfer_calls) == 1
    assert transfer_calls[0]["name"] == f"transfer_to_{selected.lower()}"
    assert tool_output(responses.requests[3], "transfer") == "Handoff reserved."
    assert tool_output(responses.requests[3], "source-read") == "source-receipt"
    assert tool_output(responses.requests[3], "destination-read") == f"{selected}-receipt"
    for call_id in ("source-read", "transfer", "destination-read"):
        assert any(
            item.get("type") == "function_call" and item.get("call_id") == call_id
            for item in responses.requests[3]["input"]
        )


@pytest_asyncio.fixture(loop_scope="function")
async def controlled_realtime_peer():
    # Reuse the upstream native protocol fixture's captured frame/call helpers.
    # Its default handler immediately completes every response.create; this
    # handler instead lets this test hold provider audio active until interrupted.
    from .test_native_realtime import RealtimePeer

    peer = RealtimePeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.responses.handle)

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        assert request.query.get("model") == "fixture-realtime"
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        peer.socket = socket
        peer.connections += 1
        connection_frames: list[dict[str, Any]] = []
        peer.connection_frames.append(connection_frames)
        state: dict[str, Any] = {}
        try:
            async for message in socket:
                assert message.type == WSMsgType.TEXT
                payload = json.loads(message.data)
                async with peer.changed:
                    peer.frames.append(payload)
                    connection_frames.append(payload)
                    peer.changed.notify_all()
                if payload["type"] == "session.update":
                    peer.merge_session(state, payload["session"])
                    await socket.send_json(
                        {
                            "type": "session.updated",
                            "session": {**deepcopy(state), "id": "capability-voice"},
                        }
                    )
        finally:
            peer.socket = None
            peer.disconnected.set()
        return socket

    app.router.add_get("/v1/realtime", websocket)

    async def close_socket(_app):
        if peer.socket is not None:
            await peer.socket.close()

    app.on_shutdown.append(close_socket)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.responses.url = str(server.make_url("/v1"))
        yield peer


async def test_native_two_audio_turns_interrupt_first_and_correlate_second_tool(
    controlled_realtime_peer,
) -> None:
    from agents.realtime import (
        RealtimeAudio,
        RealtimeAudioInput,
        RealtimeConfig,
        RealtimePlaybackInterrupted,
        RealtimePlaybackPosition,
        RealtimeResponse,
    )

    peer = controlled_realtime_peer
    application = {"marker": "captured-voice-owner"}
    effects: list[tuple[str, str]] = []

    @function_tool
    async def lookup(ctx: ToolContext[dict[str, str]], key: str) -> str:
        """Read a record through the captured native voice tool owner."""
        assert ctx.context is application
        effects.append((ctx.context["marker"], key))
        return "voice-tool-receipt"

    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-capability-voice",
            supports_websockets=False,
        ) as backend,
        Agent(
            instructions="Answer with audio and use lookup when asked.",
            tools=[lookup],
            provider=backend,
        ).session(context=application) as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                tools=(lookup,),
                settings={
                    "input_format": "pcm16",
                    "output_format": "pcm16",
                    "turn_detection": None,
                },
            )
        ) as voice,
        aclosing(voice.events()) as events,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")

        async def observed(kind, predicate=lambda event: True):
            async for event in events:
                if isinstance(event, kind) and predicate(event):
                    return event
            pytest.fail(f"Voice closed before {kind.__name__}")

        async def commit_audio(data: bytes):
            start = len(peer.frames)
            await voice.send_audio(RealtimeAudioInput(data, "pcm16"), commit=True)
            await peer.frame(
                lambda frame: frame["type"] == "input_audio_buffer.commit", after=start
            )
            outgoing = peer.frames[start:]
            audio = [frame for frame in outgoing if frame["type"] == "input_audio_buffer.append"]
            assert b"".join(base64.b64decode(frame["audio"]) for frame in audio) == data
            assert sum(frame["type"] == "input_audio_buffer.commit" for frame in outgoing) == 1
            await voice.request_response()
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)

        async def output_audio(response_id: str, item_id: str, data: bytes):
            assert peer.socket is not None
            await peer.socket.send_json(
                {
                    "type": "response.output_audio.delta",
                    "response_id": response_id,
                    "item_id": item_id,
                    "content_index": 0,
                    "output_index": 0,
                    "delta": base64.b64encode(data).decode(),
                }
            )
            event = await observed(RealtimeAudio, lambda event: event.response_id == response_id)
            assert event.data == data and event.item_id == item_id
            assert event.sample_rate == 24000 and event.num_channels == 1
            assert event.format == "pcm16"

        await commit_audio(b"\x01\x00" * 2400)
        assert peer.socket is not None
        await peer.socket.send_json({"type": "response.created", "response": {"id": "speech-one"}})
        await output_audio("speech-one", "audio-one", b"\x02\x00" * 2400)
        # No response.done has been sent: first speech is still active. Report
        # 50 ms of its 100 ms audio and interrupt exactly this response.
        start = len(peer.frames)
        await voice.interrupt(
            "speech-one", playback=RealtimePlaybackPosition("speech-one", "audio-one", 0, 50)
        )
        truncated = await peer.frame(
            lambda frame: frame["type"] == "conversation.item.truncate", after=start
        )
        assert truncated["item_id"] == "audio-one" and truncated["audio_end_ms"] == 50
        cancelled = await peer.frame(lambda frame: frame["type"] == "response.cancel", after=start)
        assert cancelled.get("response_id") == "speech-one"
        interruption = await observed(RealtimePlaybackInterrupted)
        assert interruption.cancelled_response_id == "speech-one"
        assert "speech-one" in interruption.retired_response_ids
        # The provider acknowledges termination after receiving response.cancel.
        # First speech was active when interrupted; do not leave its protocol
        # response open while beginning the next committed audio turn.
        assert peer.socket is not None
        await peer.socket.send_json(
            {
                "type": "response.done",
                "response": {"id": "speech-one", "status": "cancelled", "output": []},
            }
        )

        await commit_audio(b"\x03\x00" * 2400)
        # One provider response includes both the second audio and a function
        # call. Hold response.done until its audio has crossed the native owner.
        await peer.functions(
            [("lookup", {"key": "second-audio-turn"}, "voice-call-two")],
            response_id="speech-two",
            response_done=False,
        )
        await output_audio("speech-two", "audio-two", b"\x04\x00" * 2400)
        assert peer.socket is not None
        await peer.socket.send_json(
            {
                "type": "response.done",
                "response": {
                    "id": "speech-two",
                    "status": "completed",
                    "output": [
                        {
                            "id": "item-voice-call-two",
                            "type": "function_call",
                            "name": "lookup",
                            "call_id": "voice-call-two",
                            "arguments": json.dumps({"key": "second-audio-turn"}),
                        }
                    ],
                },
            }
        )
        await observed(
            RealtimeResponse,
            lambda event: event.response_id == "speech-two" and event.status == "done",
        )
        result = await peer.frame(
            lambda frame: (
                frame["type"] == "conversation.item.create"
                and frame.get("item", {}).get("call_id") == "voice-call-two"
            )
        )
        assert result["item"]["type"] == "function_call_output"
        assert result["item"]["output"] == "voice-tool-receipt"
        # The newer response completed normally after interruption of the first;
        # its output and correlated tool result used this one live connection.
        assert effects == [("captured-voice-owner", "second-audio-turn")]
        assert peer.connections == 1 and len(peer.connection_frames) == 1
        assert peer.responses.requests == []
        assert sum(frame["type"] == "input_audio_buffer.commit" for frame in peer.frames) == 2
        assert not any(
            frame["type"] == "response.cancel" and frame.get("response_id") == "speech-two"
            for frame in peer.frames
        )
