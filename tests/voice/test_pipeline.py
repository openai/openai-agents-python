from __future__ import annotations

import asyncio

import numpy as np
import numpy.typing as npt
import pytest

try:
    from agents.voice import (
        AudioInput,
        StreamedAudioResult,
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


def test_streamed_audio_result_odd_length_buffer_int16() -> None:
    result = StreamedAudioResult(
        FakeTTS(),
        TTSModelSettings(dtype=np.int16),
        VoicePipelineConfig(),
    )

    transformed = result._transform_audio_buffer([b"\x01"], np.int16)

    assert transformed.dtype == np.int16
    assert transformed.tolist() == [1]


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
        if isinstance(event, VoiceStreamEventAudio) and event.data is not None:
            audio_chunks.append(event.data.tobytes())
        if isinstance(event, VoiceStreamEventLifecycle) and event.event == "turn_ended":
            break

    assert audio_chunks == [np.array([1], dtype=np.int16).tobytes()]


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
