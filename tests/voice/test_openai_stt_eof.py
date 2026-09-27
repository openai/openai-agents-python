"""Exercise streamed EOF through the real STT transport and public pipeline."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import pytest
from openai import AsyncOpenAI
from websockets.asyncio.server import ServerConnection, serve

from agents.voice import OpenAISTTModel, StreamedAudioInput, VoicePipeline, VoicePipelineConfig
from agents.voice.exceptions import STTWebsocketConnectionError
from agents.voice.models import openai_stt
from agents.voice.testing import ScriptedTTSModel, ScriptedVoiceWorkflow


async def _send(socket: ServerConnection, event_type: str, **fields: Any) -> None:
    await socket.send(json.dumps({"type": event_type, **fields}))


@asynccontextmanager
async def _pipeline(
    handle_input: Callable[[ServerConnection], Awaitable[None]],
    turns: int = 0,
) -> AsyncIterator[tuple[StreamedAudioInput, asyncio.Task[None], list[str], ScriptedVoiceWorkflow]]:
    socket_closed = asyncio.Event()

    async def handle_connection(socket: ServerConnection) -> None:
        try:
            await _send(socket, "session.created")
            assert json.loads(await socket.recv())["type"] == "session.update"
            await _send(socket, "session.updated")
            await handle_input(socket)
            await socket.wait_closed()
        finally:
            socket_closed.set()

    async with serve(handle_connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with AsyncOpenAI(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1"
        ) as client:
            workflow = ScriptedVoiceWorkflow(["Reply."] * turns)
            pipeline = VoicePipeline(
                workflow=workflow,
                stt_model=OpenAISTTModel("gpt-4o-transcribe", client),
                tts_model=ScriptedTTSModel([[b"\x00\x00" * 10]] * turns),
                config=VoicePipelineConfig(tracing_disabled=True),
            )
            audio = StreamedAudioInput()
            result = await pipeline.run(audio)
            events: list[str] = []

            async def consume() -> None:
                async for event in result.stream():
                    events.append(getattr(event, "event", event.type))

            consumer = asyncio.create_task(consume())
            try:
                yield audio, consumer, events, workflow
            finally:
                if not consumer.done():
                    consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
                await asyncio.wait_for(socket_closed.wait(), 2)
                assert result.text_generation_task is not None
                assert result.text_generation_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["empty", "final_buffer", "vad", "vad_race", "empty_transcript", "legacy"]
)
async def test_eof_drains_transcriptions_before_finishing(mode: str) -> None:
    cleared = asyncio.Event()
    release_transcripts = asyncio.Event()
    vad_completed = asyncio.Event()
    expected = [] if mode in {"empty", "empty_transcript"} else ["Final phrase"]
    if mode == "vad_race":
        expected = ["Second phrase", "First phrase"]

    async def handle_input(socket: ServerConnection) -> None:
        if mode != "empty":
            assert json.loads(await socket.recv())["type"] == "input_audio_buffer.append"
        if mode == "vad":
            await _send(socket, "input_audio_buffer.committed", item_id="first")
            await _send(
                socket,
                "conversation.item.input_audio_transcription.completed",
                item_id="first",
                transcript="Final phrase",
            )
            vad_completed.set()
        commit = json.loads(await socket.recv())
        assert commit["type"] == "input_audio_buffer.commit"
        if mode in {"empty", "vad", "vad_race"}:
            if mode == "vad_race":
                # VAD won the commit race, but neither transcript has completed yet.
                await _send(socket, "input_audio_buffer.committed", item_id="first")
                await _send(socket, "input_audio_buffer.committed", item_id="second")
            await _send(
                socket,
                "error",
                error={
                    "code": "input_audio_buffer_commit_empty",
                    "event_id": commit["event_id"],
                    "message": "Input buffer is empty",
                },
            )
        else:
            await _send(socket, "input_audio_buffer.committed", item_id="first")
        assert json.loads(await socket.recv())["type"] == "input_audio_buffer.clear"
        await _send(socket, "input_audio_buffer.cleared")
        cleared.set()
        if mode in {"empty", "vad"}:
            return
        await release_transcripts.wait()
        if mode == "vad_race":
            await _send(
                socket,
                "conversation.item.input_audio_transcription.completed",
                item_id="second",
                transcript="Second phrase",
            )
        if mode == "legacy":
            await _send(socket, "input_audio_transcription_completed", transcript="Final phrase")
            return
        await _send(
            socket,
            "conversation.item.input_audio_transcription.completed",
            item_id="first",
            transcript=""
            if mode == "empty_transcript"
            else "First phrase"
            if mode == "vad_race"
            else "Final phrase",
        )

    async with _pipeline(handle_input, turns=len(expected)) as (audio, consumer, events, workflow):
        try:
            if mode != "empty":
                await audio.add_audio(np.zeros(4800, dtype=np.int16))
            if mode == "vad":
                await asyncio.wait_for(vad_completed.wait(), 2)
            await audio.add_audio(None)
            await asyncio.wait_for(cleared.wait(), 2)
            if mode not in {"empty", "vad"}:
                done, _ = await asyncio.wait({consumer}, timeout=0.05)
                assert not done, "EOF must wait for the outstanding transcripts"
            release_transcripts.set()
            await asyncio.wait_for(asyncio.shield(consumer), 2)
            assert list(workflow.transcriptions) == expected
            assert events == ["turn_started", "voice_stream_event_audio", "turn_ended"] * len(
                expected
            ) + ["session_ended"]
        finally:
            release_transcripts.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["timeout", "transcription_error", "unrelated_error", "disconnect", "cancel"]
)
async def test_eof_drain_failure_and_cancellation_close_the_session(
    outcome: str, monkeypatch
) -> None:
    draining = asyncio.Event()
    release = asyncio.Event()
    if outcome == "timeout":
        monkeypatch.setattr(openai_stt, "EVENT_INACTIVITY_TIMEOUT", 0.05)

    async def handle_input(socket: ServerConnection) -> None:
        assert json.loads(await socket.recv())["type"] == "input_audio_buffer.append"
        commit = json.loads(await socket.recv())
        assert commit["type"] == "input_audio_buffer.commit"
        await _send(socket, "input_audio_buffer.committed", item_id="first")
        assert json.loads(await socket.recv())["type"] == "input_audio_buffer.clear"
        await _send(socket, "input_audio_buffer.cleared")
        draining.set()
        await release.wait()
        if outcome == "transcription_error":
            await _send(
                socket,
                "conversation.item.input_audio_transcription.failed",
                item_id="first",
                error={"message": "Synthetic failure"},
            )
        elif outcome == "unrelated_error":
            await _send(
                socket,
                "error",
                error={"code": "input_audio_buffer_commit_empty", "event_id": "unrelated"},
            )
        elif outcome == "disconnect":
            await socket.close()

    async with _pipeline(handle_input) as (audio, consumer, events, workflow):
        try:
            await audio.add_audio(np.zeros(4800, dtype=np.int16))
            await audio.add_audio(None)
            await asyncio.wait_for(draining.wait(), 2)
            if outcome == "cancel":
                consumer.cancel()
            release.set()
            error_type = (
                asyncio.CancelledError if outcome == "cancel" else STTWebsocketConnectionError
            )
            with pytest.raises(error_type):
                await asyncio.wait_for(asyncio.shield(consumer), 2)
            assert workflow.transcriptions == ()
        finally:
            release.set()


@pytest.mark.asyncio
async def test_explicit_close_aborts_pending_eof_without_a_provider_error() -> None:
    from agents.voice import OpenAISTTTranscriptionSession, STTModelSettings

    draining = asyncio.Event()
    socket_closed = asyncio.Event()

    async def handle_connection(socket: ServerConnection) -> None:
        try:
            await _send(socket, "session.created")
            await socket.recv()
            await _send(socket, "session.updated")
            assert json.loads(await socket.recv())["type"] == "input_audio_buffer.append"
            assert json.loads(await socket.recv())["type"] == "input_audio_buffer.commit"
            await _send(socket, "input_audio_buffer.committed", item_id="pending")
            assert json.loads(await socket.recv())["type"] == "input_audio_buffer.clear"
            await _send(socket, "input_audio_buffer.cleared")
            draining.set()
            await socket.wait_closed()
        finally:
            socket_closed.set()

    async with serve(handle_connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with AsyncOpenAI(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1"
        ) as client:
            audio = StreamedAudioInput()
            session = await OpenAISTTModel("gpt-4o-transcribe", client).create_session(
                audio, STTModelSettings(), False, False
            )
            assert isinstance(session, OpenAISTTTranscriptionSession)

            async def consume() -> list[str]:
                return [turn async for turn in session.transcribe_turns()]

            consumer = asyncio.create_task(consume())
            try:
                await audio.add_audio(np.zeros(4800, dtype=np.int16))
                await audio.add_audio(None)
                await asyncio.wait_for(draining.wait(), 2)
                await asyncio.wait_for(session.close(), 2)
                assert await asyncio.wait_for(asyncio.shield(consumer), 2) == []
                await asyncio.wait_for(socket_closed.wait(), 2)
                assert all(
                    task is not None and task.done()
                    for task in (
                        session._connection_task,
                        session._listener_task,
                        session._stream_audio_task,
                        session._process_events_task,
                    )
                )
            finally:
                if not consumer.done():
                    consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
                await session.close()
