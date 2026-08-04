# test_openai_stt_transcription_session.py

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import numpy.typing as npt
import pytest

import agents._debug as _debug
from agents import trace
from agents.exceptions import UserError
from tests.testing_processor import fetch_span_errors

try:
    from agents.voice import (
        AudioInput,
        OpenAISTTModel,
        OpenAISTTTranscriptionSession,
        StreamedAudioInput,
        STTModelSettings,
    )
    from agents.voice.exceptions import STTWebsocketConnectionError
    from agents.voice.models.openai_stt import (
        EVENT_INACTIVITY_TIMEOUT,
        ErrorSentinel,
        WebsocketDoneSentinel,
        _audio_buffer_to_base64,
    )

    from .fake_models import FakeStreamedAudioInput
except ImportError:
    pass


# ===== Helpers =====


def create_mock_websocket(messages: list[str]) -> AsyncMock:
    """
    Creates a mock websocket (AsyncMock) that will return the provided incoming_messages
    from __aiter__() as if they came from the server.
    """

    mock_ws = AsyncMock()
    mock_ws.__aenter__.return_value = mock_ws
    # The incoming_messages are strings that we pretend come from the server
    mock_ws.__aiter__.return_value = iter(messages)
    return mock_ws


def fake_time(increment: int):
    current = 1000
    while True:
        yield current
        current += increment


# ===== Tests =====
@pytest.mark.asyncio
async def test_transcribe_turns_propagates_consumer_cancellation(monkeypatch) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    session._websocket = AsyncMock()
    get_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def wait_for_turn() -> str:
        get_started.set()
        await never_finishes.wait()
        raise AssertionError("Unreachable")

    async def hold_connection_open() -> None:
        await never_finishes.wait()

    monkeypatch.setattr(session._output_queue, "get", wait_for_turn)
    monkeypatch.setattr(session, "_process_websocket_connection", hold_connection_open)
    consumer = asyncio.ensure_future(anext(session.transcribe_turns()))
    await get_started.wait()
    consumer.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await consumer
        session._websocket.close.assert_awaited_once()
    finally:
        await session.close()
        if session._connection_task is not None:
            await asyncio.gather(session._connection_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transcribe_turns_closes_owned_tasks_after_yield(monkeypatch) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    session._websocket = AsyncMock()
    tracing_span = MagicMock()
    session._tracing_span = tracing_span
    never_finishes = asyncio.Event()
    started = [asyncio.Event() for _ in range(4)]
    stopped = [asyncio.Event() for _ in range(4)]

    async def hold_open(index: int) -> None:
        started[index].set()
        try:
            await never_finishes.wait()
        finally:
            stopped[index].set()

    async def hold_connection_open() -> None:
        await hold_open(0)

    monkeypatch.setattr(session, "_process_websocket_connection", hold_connection_open)
    session._listener_task = asyncio.create_task(hold_open(1))
    session._process_events_task = asyncio.create_task(hold_open(2))
    session._stream_audio_task = asyncio.create_task(hold_open(3))
    await session._output_queue.put("hello")

    turns = cast(AsyncGenerator[str, None], session.transcribe_turns())
    assert await anext(turns) == "hello"
    await asyncio.gather(*(event.wait() for event in started))

    owned_tasks = (
        session._connection_task,
        session._listener_task,
        session._process_events_task,
        session._stream_audio_task,
    )
    try:
        await turns.aclose()
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in stopped)),
            timeout=1,
        )
        assert all(task is not None and task.cancelled() for task in owned_tasks)
        session._websocket.close.assert_awaited_once()
        tracing_span.finish.assert_called_once_with()
        assert session._tracing_span is None
    finally:
        tasks = [task for task in owned_tasks if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_finishes_span_started_while_websocket_close_is_pending() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    old_span = MagicMock()
    replacement_span = MagicMock()
    session._tracing_span = old_span
    websocket_close_started = asyncio.Event()
    allow_websocket_close = asyncio.Event()

    async def close_websocket() -> None:
        websocket_close_started.set()
        await allow_websocket_close.wait()

    session._websocket = AsyncMock()
    session._websocket.close.side_effect = close_websocket
    session._process_events_task = asyncio.create_task(session._handle_events())

    with patch(
        "agents.voice.models.openai_stt.transcription_span",
        return_value=replacement_span,
    ):
        close_task = asyncio.create_task(session.close())
        try:
            await websocket_close_started.wait()
            await session._event_queue.put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "late transcript",
                }
            )
            assert await session._output_queue.get() == "late transcript"
            session._output_queue.task_done()

            allow_websocket_close.set()
            await close_task
        finally:
            allow_websocket_close.set()
            if not close_task.done():
                close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)

    old_span.finish.assert_called_once_with()
    replacement_span.start.assert_called_once_with()
    replacement_span.finish.assert_called_once_with()
    assert session._tracing_span is None
    assert session._process_events_task.cancelled()


@pytest.mark.asyncio
async def test_transcribe_turns_preserves_consumer_exception_when_cleanup_fails(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    never_finishes = asyncio.Event()

    async def hold_connection_open() -> None:
        await never_finishes.wait()

    async def fail_cleanup() -> None:
        raise RuntimeError("sensitive cleanup detail")

    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(session, "_process_websocket_connection", hold_connection_open)
    monkeypatch.setattr(session, "_cleanup_tasks", fail_cleanup)
    await session._output_queue.put("hello")
    turns = cast(AsyncGenerator[str, None], session.transcribe_turns())
    assert await anext(turns) == "hello"

    try:
        with caplog.at_level(logging.WARNING, logger="openai.agents"):
            with pytest.raises(ValueError, match="sensitive consumer detail"):
                await turns.athrow(ValueError("sensitive consumer detail"))
    finally:
        if session._connection_task is not None:
            session._connection_task.cancel()
            await asyncio.gather(session._connection_task, return_exceptions=True)

    message = "STT session cleanup failed while preserving another exception"
    record = caplog.records[-1]
    assert record.msg == message
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.getMessage() == message
    assert logging.Formatter().format(record) == message
    assert all(
        not isinstance(value, RuntimeError | ValueError) for value in record.__dict__.values()
    )


@pytest.mark.asyncio
async def test_transcribe_turns_propagates_cancellation_during_cleanup(monkeypatch) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    never_finishes = asyncio.Event()

    async def hold_connection_open() -> None:
        await never_finishes.wait()

    async def cancelled_cleanup() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(session, "_process_websocket_connection", hold_connection_open)
    monkeypatch.setattr(session, "_cleanup_tasks", cancelled_cleanup)
    await session._output_queue.put("hello")
    turns = cast(AsyncGenerator[str, None], session.transcribe_turns())
    assert await anext(turns) == "hello"

    try:
        # A primary consumer exception is active, but a cancellation raised while the STT
        # session is closing must still propagate rather than be swallowed as secondary.
        with pytest.raises(asyncio.CancelledError):
            await turns.athrow(ValueError("consumer detail"))
    finally:
        if session._connection_task is not None:
            session._connection_task.cancel()
            await asyncio.gather(session._connection_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transcribe_turns_preserves_terminal_error_when_close_fails(
    monkeypatch,
) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    terminal_error = RuntimeError("terminal STT error")

    async def fail_connection() -> None:
        await session._output_queue.put(ErrorSentinel(terminal_error))
        raise terminal_error

    session._websocket = AsyncMock()
    session._websocket.close.side_effect = RuntimeError("websocket cleanup error")
    monkeypatch.setattr(session, "_process_websocket_connection", fail_connection)

    turns = session.transcribe_turns()
    with pytest.raises(RuntimeError, match="terminal STT error") as exc_info:
        await anext(turns)

    assert exc_info.value is terminal_error
    assert session._connection_task is not None
    await asyncio.gather(session._connection_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trace_include_sensitive_data", "expected_error"),
    [
        (False, "Error details are redacted."),
        (True, "sensitive-stt-error"),
    ],
)
async def test_transcribe_error_respects_sensitive_data_setting(
    trace_include_sensitive_data: bool,
    expected_error: str,
) -> None:
    client = AsyncMock()
    client.audio.transcriptions.create = AsyncMock(side_effect=RuntimeError("sensitive-stt-error"))
    model = OpenAISTTModel(model="whisper-1", openai_client=client)

    with trace("stt-error"):
        with pytest.raises(RuntimeError, match="sensitive-stt-error"):
            await model.transcribe(
                AudioInput(buffer=np.zeros(2, dtype=np.int16)),
                STTModelSettings(),
                trace_include_sensitive_data=trace_include_sensitive_data,
                trace_include_sensitive_audio_data=False,
            )

    assert fetch_span_errors("transcription") == [{"message": expected_error, "data": {}}]


@pytest.mark.asyncio
async def test_non_json_messages_should_crash():
    """This tests that non-JSON messages will raise an exception"""
    # Setup: mock websockets.connect
    mock_ws = create_mock_websocket(["not a json message"])
    with patch("websockets.connect", return_value=mock_ws):
        # Instantiate the session
        input_audio = await FakeStreamedAudioInput.get(count=2)
        stt_settings = STTModelSettings()

        session = OpenAISTTTranscriptionSession(
            input=input_audio,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=stt_settings,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

        with pytest.raises(STTWebsocketConnectionError):
            # Start reading from transcribe_turns, which triggers _process_websocket_connection
            turns = session.transcribe_turns()

            async for _ in turns:
                pass

        await session.close()


@pytest.mark.asyncio
async def test_session_connects_and_configures_successfully():
    """
    Test that the session:
    1) Connects to the correct URL with correct headers.
    2) Receives a 'session.created' event.
    3) Sends an update message for session config.
    4) Receives a 'session.updated' event.
    """
    # Setup: mock websockets.connect
    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "transcription_session.created"}),
            json.dumps({"type": "transcription_session.updated"}),
        ]
    )
    with patch("websockets.connect", return_value=mock_ws) as mock_connect:
        # Instantiate the session
        input_audio = await FakeStreamedAudioInput.get(count=2)
        stt_settings = STTModelSettings()

        session = OpenAISTTTranscriptionSession(
            input=input_audio,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=stt_settings,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

        # Start reading from transcribe_turns, which triggers _process_websocket_connection
        turns = session.transcribe_turns()

        async for _ in turns:
            pass

        # Check connect call
        args, kwargs = mock_connect.call_args
        assert "wss://api.openai.com/v1/realtime?intent=transcription" in args[0]
        headers = kwargs.get("additional_headers", {})
        assert headers.get("Authorization") == "Bearer FAKE_KEY"
        assert headers.get("OpenAI-Beta") is None
        assert headers.get("OpenAI-Log-Session") == "1"

        # Check that we sent a 'session.update' message
        sent_messages = [call.args[0] for call in mock_ws.send.call_args_list]
        assert any('"type": "session.update"' in msg for msg in sent_messages), (
            f"Expected 'session.update' in {sent_messages}"
        )

        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("buffer", "expected_pcm16"),
    [
        (
            np.array([1, 2, 3, 4], dtype=np.int16),
            np.array([1, 2, 3, 4], dtype=np.int16),
        ),
        (
            np.array([-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5], dtype=np.float32),
            np.array([-32767, -32767, -16383, 0, 16383, 32767, 32767], dtype=np.int16),
        ),
    ],
    ids=["int16", "float32"],
)
async def test_stream_audio_sends_pcm16(
    buffer: npt.NDArray[np.int16 | np.float32],
    expected_pcm16: npt.NDArray[np.int16],
) -> None:
    """
    Test that when audio is placed on the input queue, the session:
    1) Base64-encodes the data.
    2) Sends the correct JSON message over the websocket.
    """
    mock_ws = create_mock_websocket([])
    audio_input = StreamedAudioInput()
    stt_settings = STTModelSettings()

    session = OpenAISTTTranscriptionSession(
        input=audio_input,
        client=AsyncMock(api_key="FAKE_KEY"),
        model="whisper-1",
        settings=stt_settings,
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    session._websocket = mock_ws

    original_buffer = buffer.copy()
    queue: asyncio.Queue[npt.NDArray[np.int16 | np.float32] | None] = asyncio.Queue()
    await queue.put(buffer)
    await queue.put(None)

    await session._stream_audio(queue)

    append_messages = [
        json.loads(call.args[0])
        for call in mock_ws.send.call_args_list
        if '"type": "input_audio_buffer.append"' in call.args[0]
    ]
    assert len(append_messages) == 1, "No 'input_audio_buffer.append' message was sent."
    assert append_messages[0]["type"] == "input_audio_buffer.append"
    assert base64.b64decode(append_messages[0]["audio"]) == expected_pcm16.tobytes()
    np.testing.assert_array_equal(buffer, original_buffer)

    await session.close()


@pytest.mark.parametrize("dtype", [np.int32, np.float64], ids=["int32", "float64"])
def test_stream_audio_rejects_unsupported_dtype(dtype: npt.DTypeLike) -> None:
    buffer = np.array([1, 2], dtype=dtype)

    with pytest.raises(UserError, match="Buffer must be a numpy array of int16 or float32"):
        _audio_buffer_to_base64(buffer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created,updated,completed",
    [
        (
            {"type": "transcription_session.created"},
            {"type": "transcription_session.updated"},
            {"type": "input_audio_transcription_completed", "transcript": "Hello world!"},
        ),
        (
            {"type": "session.created"},
            {"type": "session.updated"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Hello world!",
            },
        ),
    ],
)
async def test_transcription_event_puts_output_in_queue(created, updated, completed):
    """
    Test that a 'input_audio_transcription_completed' event and
    'conversation.item.input_audio_transcription.completed'
    yields a transcript from transcribe_turns().
    """
    mock_ws = create_mock_websocket(
        [
            json.dumps(created),
            json.dumps(updated),
            json.dumps(completed),
        ]
    )

    with patch("websockets.connect", return_value=mock_ws):
        # Prepare
        audio_input = await FakeStreamedAudioInput.get(count=2)
        stt_settings = STTModelSettings()

        session = OpenAISTTTranscriptionSession(
            input=audio_input,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=stt_settings,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
        turns = session.transcribe_turns()

        # We'll collect transcribed turns in a list
        collected_turns = []
        async for turn in turns:
            collected_turns.append(turn)
        await session.close()

        # Check we got "Hello world!"
        assert "Hello world!" in collected_turns
        # Cleanup


@pytest.mark.asyncio
async def test_timeout_waiting_for_created_event(monkeypatch):
    """
    If the 'session.created' event does not arrive before SESSION_CREATION_TIMEOUT,
    the session should raise a TimeoutError.
    """
    time_gen = fake_time(increment=30)  # increment by 30 seconds each time

    # Define a replacement function that returns the next time
    def fake_time_func():
        return next(time_gen)

    # Monkey-patch time.time with our fake_time_func
    monkeypatch.setattr(time, "time", fake_time_func)

    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "unknown"}),
        ]
    )  # add a fake event to the mock websocket to make sure it doesn't raise a different exception

    with patch("websockets.connect", return_value=mock_ws):
        audio_input = await FakeStreamedAudioInput.get(count=2)
        stt_settings = STTModelSettings()

        session = OpenAISTTTranscriptionSession(
            input=audio_input,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=stt_settings,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
        turns = session.transcribe_turns()

        # We expect an exception once the generator tries to connect + wait for event
        with pytest.raises(STTWebsocketConnectionError) as exc_info:
            async for _ in turns:
                pass

        assert "Timeout waiting for transcription_session.created event" in str(exc_info.value)

        await session.close()


@pytest.mark.asyncio
async def test_session_error_event(monkeypatch: pytest.MonkeyPatch):
    """
    If the session receives an event with "type": "error", it should emit preceding transcripts,
    drain the event processor, and then propagate an exception.
    """
    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "transcription_session.created"}),
            json.dumps({"type": "transcription_session.updated"}),
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "Transcript before error",
                }
            ),
            # Then an error from the server
            json.dumps({"type": "error", "error": "Simulated server error!"}),
        ]
    )
    monkeypatch.setattr(
        "agents.voice.models.openai_stt.EVENT_INACTIVITY_TIMEOUT",
        0.1,
    )

    with patch("websockets.connect", return_value=mock_ws):
        audio_input = await FakeStreamedAudioInput.get(count=2)
        stt_settings = STTModelSettings()

        session = OpenAISTTTranscriptionSession(
            input=audio_input,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=stt_settings,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
        event_queue_put = AsyncMock(wraps=session._event_queue.put)
        monkeypatch.setattr(session._event_queue, "put", event_queue_put)

        collected_turns: list[str] = []
        with pytest.raises(STTWebsocketConnectionError):
            turns = session.transcribe_turns()
            async for turn in turns:
                collected_turns.append(turn)

        assert collected_turns == ["Transcript before error"]
        assert any(
            isinstance(call.args[0], WebsocketDoneSentinel)
            for call in event_queue_put.await_args_list
        )
        await session.close()
        assert session._process_events_task is not None
        assert session._process_events_task.done()
        assert not session._process_events_task.cancelled()


@pytest.mark.asyncio
async def test_session_error_event_before_session_created():
    mock_ws = create_mock_websocket(
        [json.dumps({"type": "error", "error": "Simulated setup error!"})]
    )

    with patch("websockets.connect", return_value=mock_ws):
        audio_input = await FakeStreamedAudioInput.get(count=2)
        session = OpenAISTTTranscriptionSession(
            input=audio_input,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=STTModelSettings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

        async def consume_turns() -> None:
            async for _ in session.transcribe_turns():
                pass

        with pytest.raises(STTWebsocketConnectionError):
            await asyncio.wait_for(consume_turns(), timeout=1)

        assert session._process_events_task is not None
        assert session._process_events_task.done()
        assert not session._process_events_task.cancelled()


@pytest.mark.asyncio
async def test_listener_timeout_drains_buffered_transcript_before_setup():
    messages = [
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Transcript before listener timeout",
            }
        )
    ]

    async def messages_then_timeout() -> AsyncGenerator[str, None]:
        for message in messages:
            yield message
        raise TimeoutError("Simulated listener timeout")

    mock_ws = AsyncMock()
    mock_ws.__aenter__.return_value = mock_ws
    mock_ws.__aiter__.side_effect = messages_then_timeout

    with patch("websockets.connect", return_value=mock_ws):
        audio_input = await FakeStreamedAudioInput.get(count=2)
        session = OpenAISTTTranscriptionSession(
            input=audio_input,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=STTModelSettings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

        collected_turns: list[str] = []
        with pytest.raises(STTWebsocketConnectionError):
            async for turn in session.transcribe_turns():
                collected_turns.append(turn)

        assert collected_turns == ["Transcript before listener timeout"]
        assert session._process_events_task is not None
        assert session._process_events_task.done()
        assert not session._process_events_task.cancelled()


@pytest.mark.asyncio
async def test_inactivity_timeout():
    """
    Test that if no events arrive in EVENT_INACTIVITY_TIMEOUT ms,
    _handle_events breaks out and a SessionCompleteSentinel is placed in the output queue.
    """
    # We'll feed only the creation + updated events. Then do nothing.
    # The handle_events loop should eventually time out.
    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "unknown"}),
            json.dumps({"type": "unknown"}),
            json.dumps({"type": "transcription_session.created"}),
            json.dumps({"type": "transcription_session.updated"}),
        ]
    )

    # We'll artificially manipulate the "time" to simulate inactivity quickly.
    # The code checks time.time() for inactivity over EVENT_INACTIVITY_TIMEOUT.
    # We'll increment the return_value manually.
    with (
        patch("websockets.connect", return_value=mock_ws),
        patch(
            "time.time",
            side_effect=[
                1000.0,
                1000.0 + EVENT_INACTIVITY_TIMEOUT + 1,
                2000.0 + EVENT_INACTIVITY_TIMEOUT + 1,
                3000.0 + EVENT_INACTIVITY_TIMEOUT + 1,
                9999,
            ],
        ),
    ):
        audio_input = await FakeStreamedAudioInput.get(count=2)
        stt_settings = STTModelSettings()

        session = OpenAISTTTranscriptionSession(
            input=audio_input,
            client=AsyncMock(api_key="FAKE_KEY"),
            model="whisper-1",
            settings=stt_settings,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

        collected_turns: list[str] = []
        with pytest.raises(STTWebsocketConnectionError) as exc_info:
            async for turn in session.transcribe_turns():
                collected_turns.append(turn)

        assert "Timeout waiting for transcription_session" in str(exc_info.value)

        assert len(collected_turns) == 0, "No transcripts expected, but we got something?"

        await session.close()
