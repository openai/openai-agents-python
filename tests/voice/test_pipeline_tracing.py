from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from contextvars import ContextVar, Token
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from openai import AsyncOpenAI

from agents import custom_span, get_current_span, get_current_trace, trace
from agents.tracing.processors import BackendSpanExporter
from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.setup import get_trace_provider, set_trace_provider
from agents.tracing.spans import Span, TSpanData
from agents.tracing.traces import NoOpTrace, Trace
from agents.voice import (
    AudioInput,
    StreamedAudioInput,
    STTModelSettings,
    TTSModelSettings,
    VoicePipeline,
    VoicePipelineConfig,
    VoiceWorkflowBase,
)
from agents.voice.models.openai_stt import OpenAISTTModel
from agents.voice.models.openai_tts import OpenAITTSModel
from tests.testing_processor import (
    SPAN_PROCESSOR_TESTING,
    fetch_events,
    fetch_ordered_spans,
    fetch_traces,
)

_PCM = np.array([1001, -1002, 1003, -1004], dtype=np.int16)
_ENCODED = base64.b64encode(_PCM.tobytes()).decode()
_TRANSCRIPT = "synthetic transcript sentinel"
_TEXT = "synthetic speech sentinel."
_PROMPT = "synthetic STT prompt sentinel"
_INSTRUCTIONS = "synthetic TTS instructions sentinel"


class _Workflow(VoiceWorkflowBase):
    async def run(self, transcription: str) -> AsyncIterator[str]:
        assert transcription == _TRANSCRIPT
        parent = get_current_span()
        if parent is not None:
            parent.set_error({"message": "workflow diagnostic", "data": {"text": transcription}})
        with custom_span("workflow", data={"transcript": transcription}, parent=parent):
            yield _TEXT


class _SpeechResponse:
    async def __aenter__(self) -> _SpeechResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        yield _PCM.tobytes()


class _Websocket:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.queue.put_nowait({"type": "session.created"})
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        if payload["type"] == "session.update":
            self.queue.put_nowait({"type": "session.updated"})
        else:
            assert payload["type"] == "input_audio_buffer.append"
            assert payload["audio"] == _ENCODED
            self.queue.put_nowait(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": _TRANSCRIPT,
                }
            )
            self.queue.put_nowait(None)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> _Websocket:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __aiter__(self) -> _Websocket:
        return self

    async def __anext__(self) -> str:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)


@pytest.fixture
async def speech_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Any, ...]]:
    async with AsyncOpenAI(api_key="synthetic-placeholder") as client:
        transcribe = AsyncMock(return_value=SimpleNamespace(text=_TRANSCRIPT))
        speech = MagicMock(return_value=_SpeechResponse())
        websocket = _Websocket()
        monkeypatch.setattr(client.audio.transcriptions, "create", transcribe)
        monkeypatch.setattr(client.audio.speech.with_streaming_response, "create", speech)
        monkeypatch.setattr(
            "agents.voice.models.openai_stt.websockets.connect",
            MagicMock(return_value=websocket),
        )
        yield client, transcribe, speech, websocket


async def _input(streamed: bool) -> AudioInput | StreamedAudioInput:
    if not streamed:
        return AudioInput(_PCM.copy())
    audio = StreamedAudioInput()
    await audio.add_audio(_PCM.copy())
    await audio.add_audio(None)
    return audio


def _pipeline(client: AsyncOpenAI, config: VoicePipelineConfig) -> VoicePipeline:
    return VoicePipeline(
        workflow=_Workflow(),
        stt_model=OpenAISTTModel("gpt-4o-transcribe", client),
        tts_model=OpenAITTSModel("gpt-4o-mini-tts", client),
        config=config,
    )


def _export_payload(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    http_client = MagicMock()
    http_client.post.return_value.status_code = 200
    monkeypatch.setattr("agents.tracing.processors.httpx2.Client", lambda **kwargs: http_client)
    exporter = BackendSpanExporter(api_key="synthetic-placeholder")
    exporter.export([*fetch_traces(), *fetch_ordered_spans()])
    return http_client.post.call_args.kwargs["json"]["data"] if http_client.post.called else []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"trace_include_sensitive_data": False},
        {"trace_include_sensitive_audio_data": False},
        {"trace_include_sensitive_data": False, "trace_include_sensitive_audio_data": False},
        {"tracing_disabled": True},
    ],
)
async def test_voice_tracing_controls_under_caller_span(
    streamed: bool,
    settings: dict[str, Any],
    speech_client: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, transcribe, speech, websocket = speech_client
    config = VoicePipelineConfig(
        **settings,
        stt_settings=STTModelSettings(prompt=_PROMPT),
        tts_settings=TTSModelSettings(instructions=_INSTRUCTIONS, buffer_size=1),
    )
    pipeline = _pipeline(client, config)
    with trace("caller") as caller, custom_span("parent") as parent:
        result = await pipeline.run(await _input(streamed))
        events = [event async for event in result.stream()]
        assert get_current_trace() is caller
        assert get_current_span() is parent
        assert "trace_end" not in fetch_events()
        with custom_span("sibling"):
            pass
    assert result.total_output_text == _TEXT
    assert (
        b"".join(
            cast(np.ndarray[Any, Any], event.data).tobytes()
            for event in events
            if event.type == "voice_stream_event_audio"
        )
        == _PCM.tobytes()
    )
    assert speech.call_args.kwargs["input"] == _TEXT
    assert speech.call_args.kwargs["extra_body"]["instructions"] == _INSTRUCTIONS
    if streamed:
        assert websocket.sent[0]["session"]["audio"]["input"]["transcription"]["prompt"] == _PROMPT
        assert websocket.closed
    else:
        assert transcribe.call_args.kwargs["prompt"] == _PROMPT
        assert transcribe.call_args.kwargs["file"][1].getvalue().endswith(_PCM.tobytes())

    payload = _export_payload(monkeypatch)
    assert len([item for item in payload if item["object"] == "trace"]) == 1
    spans = [item for item in payload if item["object"] == "trace.span"]
    sibling = next(item for item in spans if item["span_data"].get("name") == "sibling")
    assert sibling["trace_id"] == caller.trace_id
    assert sibling["parent_id"] == parent.span_id
    if config.tracing_disabled:
        assert parent.error is None
        assert {item["span_data"].get("name") for item in spans} == {"parent", "sibling"}
        assert all(value not in json.dumps(payload) for value in (_TRANSCRIPT, _TEXT, _ENCODED))
    else:
        voice_spans = [item for item in spans if item["span_data"]["type"] != "custom"]
        assert {item["span_data"]["type"] for item in voice_spans} == {
            "transcription",
            "speech",
            "speech_group",
        }
        assert all(item["trace_id"] == caller.trace_id for item in voice_spans)
        voice_payload = json.dumps(voice_spans)
        for sentinel in (_TRANSCRIPT, _TEXT, _PROMPT, _INSTRUCTIONS):
            assert (sentinel in voice_payload) == config.trace_include_sensitive_data
        assert (_ENCODED in voice_payload) == config.trace_include_sensitive_audio_data
        # Workflow telemetry has its own policy and is not redacted by the voice text flag.
        assert _TRANSCRIPT in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_disabled_voice_pipeline_preserves_caller_lifecycle(
    streamed: bool,
    outcome: str,
    speech_client: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _, websocket = speech_client
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("synthetic workflow failure")

    class ControlledWorkflow(VoiceWorkflowBase):
        async def run(self, transcription: str) -> AsyncIterator[str]:
            current_span = get_current_span()
            if current_span is not None:
                current_span.set_error({"message": "workflow diagnostic", "data": None})
            with custom_span("workflow", parent=current_span):
                entered.set()
                await release.wait()
                if outcome == "error":
                    raise failure
                yield _TEXT

    pipeline = _pipeline(client, VoicePipelineConfig(tracing_disabled=True))
    pipeline.workflow = ControlledWorkflow()
    # Success without a parent also checks the existing standalone opt-out.
    with (
        trace("caller") if outcome != "success" else nullcontext(),
        custom_span("parent") if outcome != "success" else nullcontext() as parent,
    ):
        caller = get_current_trace()
        result = await pipeline.run(await _input(streamed))

        async def consume() -> None:
            async for _ in result.stream():
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        assert get_current_trace() is caller
        assert get_current_span() is parent
        if caller is not None:
            with custom_span("concurrent sibling"):
                pass
        if outcome == "cancel":
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await consumer
        else:
            release.set()
            if outcome == "error":
                with pytest.raises(RuntimeError) as exc_info:
                    _ = await consumer
                assert exc_info.value is failure
            else:
                _ = await consumer
        assert get_current_trace() is caller
        assert get_current_span() is parent
        if parent is not None:
            assert parent.error is None
        if caller is not None:
            assert "trace_end" not in fetch_events()
            with custom_span("after sibling"):
                pass
    if streamed:
        assert websocket.closed
    payload = _export_payload(monkeypatch)
    spans = [item for item in payload if item["object"] == "trace.span"]
    assert {item["span_data"].get("name") for item in spans} == (
        {"parent", "concurrent sibling", "after sibling"} if caller is not None else set()
    )
    if caller is not None:
        assert all(item["trace_id"] == caller.trace_id for item in spans)
    else:
        assert not payload


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_disabled_voice_pipeline_uses_configured_provider(
    streamed: bool,
    speech_client: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_context: ContextVar[bool] = ContextVar("provider_disabled", default=False)
    automatic_span_contexts: list[bool] = []
    restored_contexts: list[bool] = []

    class DisabledTrace(NoOpTrace):
        token: Token[bool] | None = None

        def start(self, mark_as_current: bool = False) -> None:
            super().start(mark_as_current)
            if mark_as_current:
                self.token = disabled_context.set(True)

        def finish(self, reset_current: bool = False) -> None:
            try:
                super().finish(reset_current)
            finally:
                if reset_current and self.token is not None:
                    disabled_context.reset(self.token)
                    self.token = None
                    restored_contexts.append(disabled_context.get())

    class Provider(DefaultTraceProvider):
        def create_trace(self, *args: Any, **kwargs: Any) -> Trace:
            if kwargs.get("disabled"):
                return DisabledTrace()
            return super().create_trace(*args, **kwargs)

        def create_span(
            self,
            span_data: TSpanData,
            span_id: str | None = None,
            parent: Trace | Span[Any] | None = None,
            disabled: bool = False,
        ) -> Span[TSpanData]:
            if span_data.type in {"transcription", "speech", "speech_group"}:
                automatic_span_contexts.append(disabled_context.get())
            return super().create_span(
                span_data, span_id, parent, disabled or disabled_context.get()
            )

    original_provider = get_trace_provider()
    provider = Provider()
    provider.set_processors([SPAN_PROCESSOR_TESTING])
    set_trace_provider(provider)
    try:
        client, _, _, websocket = speech_client
        pipeline = _pipeline(client, VoicePipelineConfig(tracing_disabled=True))
        with trace("caller") as caller, custom_span("parent") as parent:
            result = await pipeline.run(await _input(streamed))
            _ = [event async for event in result.stream()]
            assert result.total_output_text == _TEXT
            assert automatic_span_contexts and all(automatic_span_contexts)
            assert get_current_trace() is caller
            assert get_current_span() is parent
            assert parent.error is None
            assert not disabled_context.get()
            with custom_span("sibling"):
                pass
        assert restored_contexts == [False]
        if streamed:
            assert websocket.closed
        payload = _export_payload(monkeypatch)
        assert {
            item["span_data"].get("name") for item in payload if item["object"] == "trace.span"
        } == {
            "parent",
            "sibling",
        }
        assert all(value not in json.dumps(payload) for value in (_TRANSCRIPT, _TEXT, _ENCODED))
    finally:
        set_trace_provider(original_provider)
