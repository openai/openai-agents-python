from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pytest

import agents._debug as _debug
from agents import trace
from tests.testing_processor import fetch_events, fetch_span_errors

try:
    from agents.voice import (
        AudioInput,
        StreamedAudioResult,
        STTModelSettings,
        TTSModelSettings,
        VoicePipeline,
        VoicePipelineConfig,
        VoiceStreamEvent,
        VoiceStreamEventAudio,
        VoiceStreamEventLifecycle,
    )

    from .fake_models import FakeStreamedAudioInput, FakeSTT, FakeTTS, FakeWorkflow
    from .helpers import extract_events
except ImportError:
    pass


@dataclass
class _ProviderSTTModelSettings(STTModelSettings):
    provider_language: str | None = None


@dataclass
class _ProviderTTSModelSettings(TTSModelSettings):
    provider_voice: str | None = None


@dataclass
class _ProviderVoicePipelineConfig(VoicePipelineConfig):
    stt_settings: _ProviderSTTModelSettings = field(default_factory=_ProviderSTTModelSettings)
    tts_settings: _ProviderTTSModelSettings = field(default_factory=_ProviderTTSModelSettings)


def test_streamed_audio_result_odd_length_buffer_int16() -> None:
    result = StreamedAudioResult(
        FakeTTS(),
        TTSModelSettings(dtype=np.int16),
        VoicePipelineConfig(),
    )

    transformed = result._transform_audio_buffer([b"\x01"], np.int16)

    assert transformed.dtype == np.int16
    assert transformed.tolist() == [1]


@pytest.mark.asyncio
async def test_streamed_audio_result_propagates_consumer_cancellation(monkeypatch) -> None:
    result = StreamedAudioResult(
        FakeTTS(),
        TTSModelSettings(),
        VoicePipelineConfig(),
    )
    get_started = asyncio.Event()
    never_finishes = asyncio.Event()
    producer_started = asyncio.Event()
    producer_stopped = asyncio.Event()

    async def wait_for_event() -> VoiceStreamEvent:
        get_started.set()
        await never_finishes.wait()
        raise AssertionError("Unreachable")

    async def produce_events() -> None:
        producer_started.set()
        try:
            await never_finishes.wait()
        finally:
            producer_stopped.set()

    producer = asyncio.create_task(produce_events())
    result._tasks.append(producer)
    monkeypatch.setattr(result._queue, "get", wait_for_event)
    consumer = asyncio.ensure_future(anext(result.stream()))
    await asyncio.gather(get_started.wait(), producer_started.wait())
    consumer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer
    await producer_stopped.wait()
    assert producer.cancelled()


def test_voice_pipeline_config_normalizes_dictionary_settings() -> None:
    config = VoicePipelineConfig(
        stt_settings={"language": "ja", "temperature": 0.0},
        tts_settings={"voice": "alloy", "buffer_size": 1},
    )

    assert isinstance(config.stt_settings, STTModelSettings)
    assert config.stt_settings.language == "ja"
    assert config.stt_settings.temperature == 0.0
    assert isinstance(config.tts_settings, TTSModelSettings)
    assert config.tts_settings.voice == "alloy"
    assert config.tts_settings.buffer_size == 1


def test_voice_pipeline_config_subclass_uses_declared_settings_types() -> None:
    config = _ProviderVoicePipelineConfig(
        stt_settings={"provider_language": "ja"},  # type: ignore[arg-type]
        tts_settings={"provider_voice": "voice"},  # type: ignore[arg-type]
    )

    assert isinstance(config.stt_settings, _ProviderSTTModelSettings)
    assert config.stt_settings.provider_language == "ja"
    assert isinstance(config.tts_settings, _ProviderTTSModelSettings)
    assert config.tts_settings.provider_voice == "voice"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"stt_settings": {"languge": "ja"}}, "Unknown voice.stt settings: languge"),
        ({"tts_settings": {"voce": "alloy"}}, "Unknown voice.tts settings: voce"),
    ],
)
def test_voice_pipeline_config_rejects_unknown_dictionary_settings(
    settings: dict[str, Any], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        VoicePipelineConfig(**settings)


@pytest.mark.asyncio
async def test_voicepipeline_normalizes_nested_dictionary_config() -> None:
    fake_stt = FakeSTT(["first"])
    fake_tts = FakeTTS()
    pipeline = VoicePipeline(
        workflow=FakeWorkflow([["out_1"]]),
        stt_model=fake_stt,
        tts_model=fake_tts,
        config={
            "stt_settings": {"language": "ja"},
            "tts_settings": {"voice": "alloy", "buffer_size": 1},
        },
    )

    result = await pipeline.run(AudioInput(buffer=np.zeros(2, dtype=np.int16)))
    events, audio_chunks = await extract_events(result)

    assert isinstance(pipeline.config, VoicePipelineConfig)
    assert events == ["turn_started", "audio", "turn_ended", "session_ended"]
    await fake_tts.verify_audio("out_1", audio_chunks[0])


def test_streamed_audio_result_odd_length_buffer_float32() -> None:
    result = StreamedAudioResult(
        FakeTTS(),
        TTSModelSettings(dtype=np.float32),
        VoicePipelineConfig(),
    )

    transformed = result._transform_audio_buffer([b"\x01"], np.float32)

    assert transformed.dtype == np.float32
    assert transformed.shape == (1, 1)
    assert transformed[0, 0] == pytest.approx(1 / 32767.0)


@pytest.mark.asyncio
async def test_streamed_audio_result_preserves_cross_chunk_sample_boundaries() -> None:
    class SplitSampleTTS(FakeTTS):
        async def run(self, text: str, settings: TTSModelSettings):
            del text, settings
            yield b"\x01"
            yield b"\x00"

    result = StreamedAudioResult(
        SplitSampleTTS(),
        TTSModelSettings(buffer_size=1, dtype=np.int16),
        VoicePipelineConfig(),
    )
    local_queue: asyncio.Queue[VoiceStreamEvent | None] = asyncio.Queue()

    await result._stream_audio("hello", local_queue, finish_turn=True)

    audio_chunks: list[bytes] = []
    while True:
        event = await local_queue.get()
        assert event is not None
        if isinstance(event, VoiceStreamEventAudio) and event.data is not None:
            audio_chunks.append(event.data.tobytes())
        if isinstance(event, VoiceStreamEventLifecycle) and event.event == "turn_ended":
            break

    assert audio_chunks == [np.array([1], dtype=np.int16).tobytes()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trace_include_sensitive_data", "expected_error"),
    [
        (False, "Error details are redacted."),
        (True, "sensitive-tts-error"),
    ],
)
async def test_streamed_audio_error_respects_sensitive_data_setting(
    trace_include_sensitive_data: bool,
    expected_error: str,
) -> None:
    class FailingTTS(FakeTTS):
        async def run(self, text: str, settings: TTSModelSettings):
            del text, settings
            raise RuntimeError("sensitive-tts-error")
            yield b""  # pragma: no cover

    result = StreamedAudioResult(
        FailingTTS(),
        TTSModelSettings(),
        VoicePipelineConfig(trace_include_sensitive_data=trace_include_sensitive_data),
    )
    local_queue: asyncio.Queue[VoiceStreamEvent | None] = asyncio.Queue()

    with trace("tts-error"):
        with pytest.raises(RuntimeError, match="sensitive-tts-error"):
            await result._stream_audio("sensitive input", local_queue)

    assert fetch_span_errors("speech") == [
        {
            "message": expected_error,
            "data": {
                "text": "sensitive input" if trace_include_sensitive_data else "",
            },
        }
    ]


@pytest.mark.asyncio
async def test_streamed_audio_dispatcher_handles_stream_failure() -> None:
    """A failed _stream_audio task must not leave _dispatch_audio blocked forever."""

    class FailingTTS(FakeTTS):
        async def run(self, text: str, settings: TTSModelSettings):
            del text, settings
            raise RuntimeError("tts-failure")
            yield b""  # pragma: no cover

    result = StreamedAudioResult(
        FailingTTS(),
        TTSModelSettings(),
        VoicePipelineConfig(trace_include_sensitive_data=False),
    )

    await result._add_text("This is the first sentence. This is the second one.")

    with pytest.raises(RuntimeError, match="tts-failure"):
        await result._turn_done()

    # The single-turn pipeline queues the error and re-raises without calling _done(),
    # so _completed_session stays false. The dispatcher must return as soon as it
    # forwards the session_ended sentinel from the failed segment instead of blocking
    # on the dead queue (or spinning in the outer wait loop).
    dispatcher_task = result._dispatcher_task
    assert dispatcher_task is not None
    await asyncio.wait_for(dispatcher_task, timeout=5.0)
    assert dispatcher_task.done()

    # The failed segment's session_ended is forwarded once; the dispatcher's normal
    # epilogue must not queue a second terminal event.
    events: list[VoiceStreamEvent] = []
    while True:
        try:
            events.append(result._queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    terminal_events = [
        event
        for event in events
        if isinstance(event, VoiceStreamEventLifecycle) and event.event == "session_ended"
    ]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_voice_pipeline_awaits_task_cleanup_after_tts_failure() -> None:
    """A public pipeline stream must await sibling task cleanup when TTS fails."""

    second_segment_started = asyncio.Event()
    second_segment_stopped = asyncio.Event()

    class FailingTTS(FakeTTS):
        async def run(self, text: str, settings: TTSModelSettings):
            del settings
            if text == "first":
                await second_segment_started.wait()
                raise RuntimeError("tts-failure")
                yield b""  # pragma: no cover

            second_segment_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                second_segment_stopped.set()

    def split_immediately(text: str) -> tuple[str, str]:
        return text, ""

    pipeline = VoicePipeline(
        workflow=FakeWorkflow([["first", "second"]]),
        stt_model=FakeSTT(["user input"]),
        tts_model=FailingTTS(),
        config=VoicePipelineConfig(tts_settings=TTSModelSettings(text_splitter=split_immediately)),
    )
    result = await pipeline.run(AudioInput(buffer=np.zeros(2, dtype=np.int16)))

    with pytest.raises(RuntimeError, match="tts-failure"):
        async for _event in result.stream():
            pass

    assert second_segment_stopped.is_set()
    assert all(task.done() for task in result._tasks)
    assert result._dispatcher_task is not None
    assert result._dispatcher_task.done()
    assert result._tracing_span is None
    assert result.text_generation_task is not None
    assert result.text_generation_task.done()


@pytest.mark.asyncio
async def test_streamed_audio_dispatcher_blocks_until_work_is_available() -> None:
    """The dispatcher must block while idle without losing a pre-wait notification."""

    class PausingEvent(asyncio.Event):
        def __init__(self) -> None:
            super().__init__()
            self.wait_calls = 0
            self.wait_started = asyncio.Event()
            self.second_wait_started = asyncio.Event()
            self.allow_wait = asyncio.Event()

        async def wait(self) -> Literal[True]:
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.wait_started.set()
                await self.allow_wait.wait()
            elif self.wait_calls == 2:
                self.second_wait_started.set()
            return await super().wait()

    def split_immediately(text: str) -> tuple[str, str]:
        return text, ""

    fake_tts = FakeTTS()
    result = StreamedAudioResult(
        fake_tts,
        TTSModelSettings(buffer_size=1, text_splitter=split_immediately),
        VoicePipelineConfig(),
    )
    dispatcher_event = PausingEvent()
    result._dispatcher_event = dispatcher_event
    dispatcher_task = asyncio.create_task(result._dispatch_audio())
    result._dispatcher_task = dispatcher_task

    try:
        await asyncio.wait_for(dispatcher_event.wait_started.wait(), timeout=1.0)
        for _ in range(10):
            await asyncio.sleep(0)
        assert dispatcher_event.wait_calls == 1
        assert not dispatcher_task.done()

        await result._add_text("ok")
        assert dispatcher_event.is_set()
        dispatcher_event.allow_wait.set()
        await result._turn_done()
        await asyncio.wait_for(dispatcher_event.second_wait_started.wait(), timeout=1.0)
        await result._done()
    finally:
        dispatcher_event.allow_wait.set()
        if not dispatcher_task.done():
            dispatcher_task.cancel()
        await asyncio.gather(dispatcher_task, return_exceptions=True)

    events, audio_chunks = await extract_events(result)

    assert events == ["turn_started", "audio", "turn_ended", "session_ended"]
    assert len(audio_chunks) == 1
    await fake_tts.verify_audio("ok", audio_chunks[0])


@pytest.mark.asyncio
async def test_streamed_audio_result_synthesizes_short_custom_splitter_chunk() -> None:
    texts: list[str] = []

    class RecordingTTS(FakeTTS):
        async def run(self, text: str, settings: TTSModelSettings):
            texts.append(text)
            yield np.zeros(2, dtype=np.int16).tobytes()

    def split_immediately(text: str) -> tuple[str, str]:
        return text, ""

    result = StreamedAudioResult(
        RecordingTTS(),
        TTSModelSettings(buffer_size=1, text_splitter=split_immediately),
        VoicePipelineConfig(),
    )

    await result._add_text("ok")
    await result._turn_done()
    await result._done()

    events, audio_chunks = await extract_events(result)

    assert texts == ["ok"]
    assert events == ["turn_started", "audio", "turn_ended", "session_ended"]
    assert audio_chunks == [np.zeros(2, dtype=np.int16).tobytes()]


@pytest.mark.asyncio
async def test_streamed_audio_result_ignores_empty_custom_splitter_chunk() -> None:
    texts: list[str] = []

    class RecordingTTS(FakeTTS):
        async def run(self, text: str, settings: TTSModelSettings):
            texts.append(text)
            yield np.zeros(2, dtype=np.int16).tobytes()

    def discard_text(_text: str) -> tuple[str, str]:
        return "", ""

    result = StreamedAudioResult(
        RecordingTTS(),
        TTSModelSettings(buffer_size=1, text_splitter=discard_text),
        VoicePipelineConfig(),
    )

    await result._add_text("ok")
    await result._turn_done()
    await result._done()

    events, audio_chunks = await extract_events(result)

    assert texts == []
    assert events == ["turn_started", "turn_ended", "session_ended"]
    assert audio_chunks == []


@pytest.mark.asyncio
async def test_voicepipeline_run_single_turn() -> None:
    # Single turn. Should produce a single audio output, which is the TTS output for "out_1".

    fake_stt = FakeSTT(["first"])
    workflow = FakeWorkflow([["out_1"]])
    fake_tts = FakeTTS()
    config = VoicePipelineConfig(tts_settings=TTSModelSettings(buffer_size=1))
    pipeline = VoicePipeline(
        workflow=workflow, stt_model=fake_stt, tts_model=fake_tts, config=config
    )
    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16))
    result = await pipeline.run(audio_input)
    events, audio_chunks = await extract_events(result)
    assert events == [
        "turn_started",
        "audio",
        "turn_ended",
        "session_ended",
    ]
    await fake_tts.verify_audio("out_1", audio_chunks[0])


@pytest.mark.asyncio
async def test_voicepipeline_streamed_audio_input() -> None:
    # Multi turn. Should produce 2 audio outputs, which are the TTS outputs of "out_1" and "out_2"

    fake_stt = FakeSTT(["first", "second"])
    workflow = FakeWorkflow([["out_1"], ["out_2"]])
    fake_tts = FakeTTS()
    pipeline = VoicePipeline(workflow=workflow, stt_model=fake_stt, tts_model=fake_tts)

    streamed_audio_input = await FakeStreamedAudioInput.get(count=2)

    result = await pipeline.run(streamed_audio_input)
    events, audio_chunks = await extract_events(result)
    assert events == [
        "turn_started",
        "audio",  # out_1
        "turn_ended",
        "turn_started",
        "audio",  # out_2
        "turn_ended",
        "session_ended",
    ]
    assert len(audio_chunks) == 2
    await fake_tts.verify_audio("out_1", audio_chunks[0])
    await fake_tts.verify_audio("out_2", audio_chunks[1])


@pytest.mark.asyncio
async def test_voicepipeline_run_single_turn_split_words() -> None:
    # Single turn. Should produce multiple audio outputs, which are the TTS outputs of "foo bar baz"
    # split into words and then "foo2 bar2 baz2" split into words.

    fake_stt = FakeSTT(["first"])
    workflow = FakeWorkflow([["foo bar baz"]])
    fake_tts = FakeTTS(strategy="split_words")
    config = VoicePipelineConfig(tts_settings=TTSModelSettings(buffer_size=1))
    pipeline = VoicePipeline(
        workflow=workflow, stt_model=fake_stt, tts_model=fake_tts, config=config
    )
    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16))
    result = await pipeline.run(audio_input)
    events, audio_chunks = await extract_events(result)
    assert events == [
        "turn_started",
        "audio",  # foo
        "audio",  # bar
        "audio",  # baz
        "turn_ended",
        "session_ended",
    ]
    await fake_tts.verify_audio_chunks("foo bar baz", audio_chunks)


@pytest.mark.asyncio
async def test_voicepipeline_run_multi_turn_split_words() -> None:
    # Multi turn. Should produce multiple audio outputs, which are the TTS outputs of "foo bar baz"
    # split into words.

    fake_stt = FakeSTT(["first", "second"])
    workflow = FakeWorkflow([["foo bar baz"], ["foo2 bar2 baz2"]])
    fake_tts = FakeTTS(strategy="split_words")
    config = VoicePipelineConfig(tts_settings=TTSModelSettings(buffer_size=1))
    pipeline = VoicePipeline(
        workflow=workflow, stt_model=fake_stt, tts_model=fake_tts, config=config
    )
    streamed_audio_input = await FakeStreamedAudioInput.get(count=6)
    result = await pipeline.run(streamed_audio_input)
    events, audio_chunks = await extract_events(result)
    assert events == [
        "turn_started",
        "audio",  # foo
        "audio",  # bar
        "audio",  # baz
        "turn_ended",
        "turn_started",
        "audio",  # foo2
        "audio",  # bar2
        "audio",  # baz2
        "turn_ended",
        "session_ended",
    ]
    assert len(audio_chunks) == 6
    await fake_tts.verify_audio_chunks("foo bar baz", audio_chunks[:3])
    await fake_tts.verify_audio_chunks("foo2 bar2 baz2", audio_chunks[3:])


@pytest.mark.asyncio
async def test_voicepipeline_float32() -> None:
    # Single turn. Should produce a single audio output, which is the TTS output for "out_1".

    fake_stt = FakeSTT(["first"])
    workflow = FakeWorkflow([["out_1"]])
    fake_tts = FakeTTS()
    config = VoicePipelineConfig(tts_settings=TTSModelSettings(buffer_size=1, dtype=np.float32))
    pipeline = VoicePipeline(
        workflow=workflow, stt_model=fake_stt, tts_model=fake_tts, config=config
    )
    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16))
    result = await pipeline.run(audio_input)
    events, audio_chunks = await extract_events(result)
    assert events == [
        "turn_started",
        "audio",
        "turn_ended",
        "session_ended",
    ]
    await fake_tts.verify_audio("out_1", audio_chunks[0], dtype=np.float32)


@pytest.mark.asyncio
async def test_voicepipeline_transform_data() -> None:
    # Single turn. Should produce a single audio output, which is the TTS output for "out_1".

    def _transform_data(
        data_chunk: npt.NDArray[np.int16 | np.float32],
    ) -> npt.NDArray[np.int16]:
        return data_chunk.astype(np.int16)

    fake_stt = FakeSTT(["first"])
    workflow = FakeWorkflow([["out_1"]])
    fake_tts = FakeTTS()
    config = VoicePipelineConfig(
        tts_settings=TTSModelSettings(
            buffer_size=1,
            dtype=np.float32,
            transform_data=_transform_data,
        )
    )
    pipeline = VoicePipeline(
        workflow=workflow, stt_model=fake_stt, tts_model=fake_tts, config=config
    )
    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16))
    result = await pipeline.run(audio_input)
    events, audio_chunks = await extract_events(result)
    assert events == [
        "turn_started",
        "audio",
        "turn_ended",
        "session_ended",
    ]
    await fake_tts.verify_audio("out_1", audio_chunks[0], dtype=np.int16)


class _BlockingWorkflow(FakeWorkflow):
    def __init__(self, gate: asyncio.Event):
        super().__init__()
        self._gate = gate

    async def run(self, _: str):
        await self._gate.wait()
        yield "out_1"


class _FailingWorkflow(FakeWorkflow):
    def __init__(self, error: BaseException):
        super().__init__()
        self.error = error

    async def run(self, _: str):
        raise self.error
        yield ""  # pragma: no cover


class _OnStartYieldThenFailWorkflow(FakeWorkflow):
    def __init__(self, outputs: list[list[str]], error: BaseException | None = None):
        super().__init__(outputs)
        self.error = error or RuntimeError("boom")

    async def on_start(self):
        yield "intro"
        raise self.error


@pytest.mark.asyncio
async def test_voicepipeline_trace_not_finished_before_single_turn_completes() -> None:
    fake_stt = FakeSTT(["first"])
    fake_tts = FakeTTS()
    gate = asyncio.Event()
    workflow = _BlockingWorkflow(gate)
    config = VoicePipelineConfig(tts_settings=TTSModelSettings(buffer_size=1))
    pipeline = VoicePipeline(
        workflow=workflow, stt_model=fake_stt, tts_model=fake_tts, config=config
    )

    audio_input = AudioInput(buffer=np.zeros(2, dtype=np.int16))
    result = await pipeline.run(audio_input)
    await asyncio.sleep(0)

    events_before_unblock = fetch_events()
    assert "trace_start" in events_before_unblock
    assert "trace_end" not in events_before_unblock

    gate.set()
    await extract_events(result)
    assert fetch_events()[-1] == "trace_end"


@pytest.mark.asyncio
async def test_voicepipeline_trace_finishes_after_multi_turn_processing() -> None:
    fake_stt = FakeSTT(["first", "second"])
    workflow = FakeWorkflow([["out_1"], ["out_2"]])
    fake_tts = FakeTTS()
    pipeline = VoicePipeline(workflow=workflow, stt_model=fake_stt, tts_model=fake_tts)

    streamed_audio_input = await FakeStreamedAudioInput.get(count=2)
    result = await pipeline.run(streamed_audio_input)
    await extract_events(result)
    assert fetch_events()[-1] == "trace_end"


@pytest.mark.asyncio
async def test_voicepipeline_multi_turn_on_start_exception_does_not_abort() -> None:
    fake_stt = FakeSTT(["first"])
    workflow = _OnStartYieldThenFailWorkflow([["out_1"]])
    fake_tts = FakeTTS()
    pipeline = VoicePipeline(workflow=workflow, stt_model=fake_stt, tts_model=fake_tts)

    streamed_audio_input = await FakeStreamedAudioInput.get(count=1)
    result = await pipeline.run(streamed_audio_input)
    events, _ = await extract_events(result)

    assert events[-1] == "session_ended"
    assert "error" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted", "redacted"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
async def test_voice_on_start_errors_apply_model_and_tool_logging_policies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    model_redacted: bool,
    tool_redacted: bool,
    redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    cause = ValueError("SECRET_VOICE_ON_START_TOOL_PAYLOAD")
    error = RuntimeError("Voice startup failed")
    error.__cause__ = cause
    pipeline = VoicePipeline(
        workflow=_OnStartYieldThenFailWorkflow([["out_1"]], error),
        stt_model=FakeSTT(["first"]),
        tts_model=FakeTTS(),
    )
    streamed_audio_input = await FakeStreamedAudioInput.get(count=1)
    caplog.set_level(logging.WARNING, logger="openai.agents")

    result = await pipeline.run(streamed_audio_input)
    events, _ = await extract_events(result)

    assert events[-1] == "session_ended"
    records = [
        record
        for record in caplog.records
        if record.name == "openai.agents"
        and (
            record.msg
            in {
                "Voice workflow on_start failed",
                "Voice workflow on_start failed: %s",
            }
            or (
                isinstance(record.args, tuple)
                and record.args
                and record.args[0] == "Voice workflow on_start failed"
            )
        )
    ]
    assert len(records) == 1
    record = records[0]
    if redacted:
        assert record.msg == "%s"
        assert record.args == ("Voice workflow on_start failed",)
        assert record.exc_info is None
        assert record.exc_text is None
        assert error not in record.__dict__.values()
        assert cause not in record.__dict__.values()
        assert "SECRET_VOICE_ON_START_TOOL_PAYLOAD" not in logging.Formatter().format(record)
    else:
        assert record.msg == "%s: %s"
        assert isinstance(record.args, tuple)
        assert error in record.args
        assert record.exc_info is not None
        assert record.exc_info[1] is error
        assert "SECRET_VOICE_ON_START_TOOL_PAYLOAD" in logging.Formatter().format(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted", "redacted"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
async def test_voice_workflow_errors_apply_model_and_tool_logging_policies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    streamed: bool,
    model_redacted: bool,
    tool_redacted: bool,
    redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    error = RuntimeError("SECRET_VOICE_TOOL_PAYLOAD")
    pipeline = VoicePipeline(
        workflow=_FailingWorkflow(error),
        stt_model=FakeSTT(["first"]),
        tts_model=FakeTTS(),
    )
    audio_input = (
        await FakeStreamedAudioInput.get(count=1)
        if streamed
        else AudioInput(buffer=np.zeros(2, dtype=np.int16))
    )
    caplog.set_level(logging.ERROR, logger="openai.agents")

    result = await pipeline.run(audio_input)
    with pytest.raises(RuntimeError, match="SECRET_VOICE_TOOL_PAYLOAD"):
        await extract_events(result)
    assert result.text_generation_task is not None
    assert result.text_generation_task.exception() is error

    expected_pipeline_message = (
        "Error processing voice turns" if streamed else "Error processing single voice turn"
    )
    records = [
        record
        for record in caplog.records
        if record.name == "openai.agents"
        and isinstance(record.args, tuple)
        and record.args
        and record.args[0] in {expected_pipeline_message, "Error processing voice output"}
    ]
    assert len(records) == 2
    for record in records:
        if redacted:
            assert record.msg == "%s"
            assert record.args in {
                (expected_pipeline_message,),
                ("Error processing voice output",),
            }
            assert record.exc_info is None
            assert "SECRET_VOICE_TOOL_PAYLOAD" not in logging.Formatter().format(record)
        else:
            assert record.msg == "%s: %s"
            assert isinstance(record.args, tuple)
            assert error in record.args
            assert record.exc_info is not None
            assert record.exc_info[1] is error
