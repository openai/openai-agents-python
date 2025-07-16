# test_openai_stt_transcription_session.py

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

try:
    from agents.voice import OpenAISTTTranscriptionSession, StreamedAudioInput, STTModelSettings
    from agents.voice.exceptions import STTWebsocketConnectionError
    from agents.voice.models.openai_stt import EVENT_INACTIVITY_TIMEOUT

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
        assert headers.get("OpenAI-Beta") == "realtime=v1"
        assert headers.get("OpenAI-Log-Session") == "1"

        # Check that we sent a 'transcription_session.update' message
        sent_messages = [call.args[0] for call in mock_ws.send.call_args_list]
        assert any('"type": "transcription_session.update"' in msg for msg in sent_messages), (
            f"Expected 'transcription_session.update' in {sent_messages}"
        )

        await session.close()


@pytest.mark.asyncio
async def test_stream_audio_sends_correct_json():
    """
    Test that when audio is placed on the input queue, the session:
    1) Base64-encodes the data.
    2) Sends the correct JSON message over the websocket.
    """
    # Simulate a single "transcription_session.created" and "transcription_session.updated" event,
    # before we test streaming.
    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "transcription_session.created"}),
            json.dumps({"type": "transcription_session.updated"}),
        ]
    )

    with patch("websockets.connect", return_value=mock_ws):
        # Prepare
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

        # Kick off the transcribe_turns generator
        turn_iter = session.transcribe_turns()
        async for _ in turn_iter:
            pass

        # Now push some audio data

        buffer1 = np.array([1, 2, 3, 4], dtype=np.int16)
        await audio_input.add_audio(buffer1)
        await asyncio.sleep(0.1)  # give time for _stream_audio to consume
        await asyncio.sleep(4)

        # Check that the websocket sent an "input_audio_buffer.append" message
        found_audio_append = False
        for call_arg in mock_ws.send.call_args_list:
            print("call_arg", call_arg)
            print("test", session._turn_audio_buffer)
            sent_str = call_arg.args[0]
            print("sent_str", sent_str)
            if '"type": "input_audio_buffer.append"' in sent_str:
                msg_dict = json.loads(sent_str)
                assert msg_dict["type"] == "input_audio_buffer.append"
                assert "audio" in msg_dict
                found_audio_append = True
        assert found_audio_append, "No 'input_audio_buffer.append' message was sent."

        await session.close()


@pytest.mark.asyncio
async def test_transcription_event_puts_output_in_queue():
    """
    Test that a 'input_audio_transcription_completed' event
    yields a transcript from transcribe_turns().
    """
    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "transcription_session.created"}),
            json.dumps({"type": "transcription_session.updated"}),
            # Once configured, we mock a completed transcription event:
            json.dumps(
                {
                    "type": "input_audio_transcription_completed",
                    "transcript": "Hello world!",
                }
            ),
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
async def test_session_error_event():
    """
    If the session receives an event with "type": "error", it should propagate an exception
    and put an ErrorSentinel in the output queue.
    """
    mock_ws = create_mock_websocket(
        [
            json.dumps({"type": "transcription_session.created"}),
            json.dumps({"type": "transcription_session.updated"}),
            # Then an error from the server
            json.dumps({"type": "error", "error": "Simulated server error!"}),
        ]
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

        with pytest.raises(STTWebsocketConnectionError):
            turns = session.transcribe_turns()
            async for _ in turns:
                pass

        await session.close()


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
