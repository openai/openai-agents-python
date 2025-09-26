import json
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
import websockets

from agents import Agent
from agents.exceptions import UserError
from agents.handoffs import handoff
from agents.realtime.model_events import (
    RealtimeModelAudioEvent,
    RealtimeModelErrorEvent,
    RealtimeModelToolCallEvent,
)
from agents.realtime.model_inputs import (
    RealtimeModelSendAudio,
    RealtimeModelSendInterrupt,
    RealtimeModelSendSessionUpdate,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
)
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel


class TestOpenAIRealtimeWebSocketModel:
    """Test suite for OpenAIRealtimeWebSocketModel connection and event handling."""

    @pytest.fixture
    def model(self):
        """Create a fresh model instance for each test."""
        return OpenAIRealtimeWebSocketModel()

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock websocket connection."""
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()
        return mock_ws


class TestConnectionLifecycle(TestOpenAIRealtimeWebSocketModel):
    """Test connection establishment, configuration, and error handling."""

    @pytest.mark.asyncio
    async def test_connect_missing_api_key_raises_error(self, model):
        """Test that missing API key raises UserError."""
        config: dict[str, Any] = {"initial_model_settings": {}}

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(UserError, match="API key is required"):
                await model.connect(config)

    @pytest.mark.asyncio
    async def test_connect_with_string_api_key(self, model, mock_websocket):
        """Test successful connection with string API key."""
        config = {
            "api_key": "test-api-key-123",
            "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
        }

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket) as mock_connect:
            with patch("asyncio.create_task") as mock_create_task:
                # Mock create_task to return a mock task and properly handle the coroutine
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    # Properly close the coroutine to avoid RuntimeWarning
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                await model.connect(config)

                # Verify WebSocket connection called with correct parameters
                mock_connect.assert_called_once()
                call_args = mock_connect.call_args
                assert (
                    call_args[0][0]
                    == "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
                )
                assert (
                    call_args[1]["additional_headers"]["Authorization"] == "Bearer test-api-key-123"
                )
                assert call_args[1]["additional_headers"].get("OpenAI-Beta") is None

                # Verify task was created for message listening
                mock_create_task.assert_called_once()

                # Verify internal state
                assert model._websocket == mock_websocket
        assert model._websocket_task is not None
        assert model.model == "gpt-4o-realtime-preview"

    @pytest.mark.asyncio
    async def test_session_update_includes_noise_reduction(self, model, mock_websocket):
        """Session.update should pass through input_audio_noise_reduction config."""
        config = {
            "api_key": "test-api-key-123",
            "initial_model_settings": {
                "model_name": "gpt-4o-realtime-preview",
                "input_audio_noise_reduction": {"type": "near_field"},
            },
        }

        sent_messages: list[dict[str, Any]] = []

        async def async_websocket(*args, **kwargs):
            async def send(payload: str):
                sent_messages.append(json.loads(payload))
                return None

            mock_websocket.send.side_effect = send
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func
                await model.connect(config)

        # Find the session.update events
        session_updates = [m for m in sent_messages if m.get("type") == "session.update"]
        assert len(session_updates) >= 1
        # Verify the last session.update contains the noise_reduction field
        session = session_updates[-1]["session"]
        assert session.get("audio", {}).get("input", {}).get("noise_reduction") == {
            "type": "near_field"
        }

    @pytest.mark.asyncio
    async def test_session_update_omits_noise_reduction_when_not_provided(
        self, model, mock_websocket
    ):
        """Session.update should omit input_audio_noise_reduction when not provided."""
        config = {
            "api_key": "test-api-key-123",
            "initial_model_settings": {
                "model_name": "gpt-4o-realtime-preview",
            },
        }

        sent_messages: list[dict[str, Any]] = []

        async def async_websocket(*args, **kwargs):
            async def send(payload: str):
                sent_messages.append(json.loads(payload))
                return None

            mock_websocket.send.side_effect = send
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func
                await model.connect(config)

        # Find the session.update events
        session_updates = [m for m in sent_messages if m.get("type") == "session.update"]
        assert len(session_updates) >= 1
        # Verify the last session.update omits the noise_reduction field
        session = session_updates[-1]["session"]
        assert "audio" in session and "input" in session["audio"]
        assert "noise_reduction" not in session["audio"]["input"]

    @pytest.mark.asyncio
    async def test_connect_with_custom_headers_overrides_defaults(self, model, mock_websocket):
        """If custom headers are provided, use them verbatim without adding defaults."""
        # Even when custom headers are provided, the implementation still requires api_key.
        config = {
            "api_key": "unused-because-headers-override",
            "headers": {"api-key": "azure-key", "x-custom": "1"},
            "url": "wss://custom.example.com/realtime?model=custom",
            # Use a valid realtime model name for session.update to validate.
            "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
        }

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket) as mock_connect:
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                await model.connect(config)

                # Verify WebSocket connection used the provided URL
                called_url = mock_connect.call_args[0][0]
                assert called_url == "wss://custom.example.com/realtime?model=custom"

                # Verify headers are exactly as provided and no defaults were injected
                headers = mock_connect.call_args.kwargs["additional_headers"]
                assert headers == {"api-key": "azure-key", "x-custom": "1"}
                assert "Authorization" not in headers
                assert "OpenAI-Beta" not in headers

    @pytest.mark.asyncio
    async def test_connect_with_callable_api_key(self, model, mock_websocket):
        """Test connection with callable API key provider."""

        def get_api_key():
            return "callable-api-key"

        config = {"api_key": get_api_key}

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                # Mock create_task to return a mock task and properly handle the coroutine
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    # Properly close the coroutine to avoid RuntimeWarning
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                await model.connect(config)
                # Should succeed with callable API key
                assert model._websocket == mock_websocket

    @pytest.mark.asyncio
    async def test_connect_with_async_callable_api_key(self, model, mock_websocket):
        """Test connection with async callable API key provider."""

        async def get_api_key():
            return "async-api-key"

        config = {"api_key": get_api_key}

        async def async_websocket(*args, **kwargs):
            return mock_websocket

        with patch("websockets.connect", side_effect=async_websocket):
            with patch("asyncio.create_task") as mock_create_task:
                # Mock create_task to return a mock task and properly handle the coroutine
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    # Properly close the coroutine to avoid RuntimeWarning
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                await model.connect(config)
                assert model._websocket == mock_websocket

    @pytest.mark.asyncio
    async def test_connect_websocket_failure_propagates(self, model):
        """Test that WebSocket connection failures are properly propagated."""
        config = {"api_key": "test-key"}

        with patch(
            "websockets.connect", side_effect=websockets.exceptions.ConnectionClosed(None, None)
        ):
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await model.connect(config)

        # Verify internal state remains clean after failure
        assert model._websocket is None
        assert model._websocket_task is None

    @pytest.mark.asyncio
    async def test_connect_already_connected_assertion(self, model, mock_websocket):
        """Test that connecting when already connected raises assertion error."""
        model._websocket = mock_websocket  # Simulate already connected

        config = {"api_key": "test-key"}

        with pytest.raises(AssertionError, match="Already connected"):
            await model.connect(config)


class TestEventHandlingRobustness(TestOpenAIRealtimeWebSocketModel):
    """Test event parsing, validation, and error handling robustness."""

    @pytest.mark.asyncio
    async def test_handle_malformed_json_logs_error_continues(self, model):
        """Test that malformed JSON emits error event but doesn't crash."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        # Malformed JSON should not crash the handler
        await model._handle_ws_event("invalid json {")

        # Should emit raw server event and error event to listeners
        assert mock_listener.on_event.call_count == 2
        error_event = mock_listener.on_event.call_args_list[1][0][0]
        assert error_event.type == "error"

    @pytest.mark.asyncio
    async def test_handle_invalid_event_schema_logs_error(self, model):
        """Test that events with invalid schema emit error events but don't crash."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        invalid_event = {"type": "response.output_audio.delta"}  # Missing required fields

        await model._handle_ws_event(invalid_event)

        # Should emit raw server event and error event to listeners
        assert mock_listener.on_event.call_count == 2
        error_event = mock_listener.on_event.call_args_list[1][0][0]
        assert error_event.type == "error"

    @pytest.mark.asyncio
    async def test_handle_unknown_event_type_ignored(self, model):
        """Test that unknown event types are ignored gracefully."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        # Create a well-formed but unknown event type
        unknown_event = {"type": "unknown.event.type", "data": "some data"}

        # Should not raise error or log anything for unknown types
        with patch("agents.realtime.openai_realtime.logger"):
            await model._handle_ws_event(unknown_event)

            # Should not log errors for unknown events (they're just ignored)
            # This will depend on the TypeAdapter validation behavior
            # If it fails validation, it should log; if it passes but type is
            # unknown, it should be ignored
            pass

    @pytest.mark.asyncio
    async def test_handle_audio_delta_event_success(self, model):
        """Test successful handling of audio delta events."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        # Set up audio format on the tracker before testing
        model._audio_state_tracker.set_audio_format("pcm16")

        # Valid audio delta event (minimal required fields for OpenAI spec)
        audio_event = {
            "type": "response.output_audio.delta",
            "event_id": "event_123",
            "response_id": "resp_123",
            "item_id": "item_456",
            "output_index": 0,
            "content_index": 0,
            "delta": "dGVzdCBhdWRpbw==",  # base64 encoded "test audio"
        }

        await model._handle_ws_event(audio_event)

        # Should emit raw server event and audio event to listeners
        assert mock_listener.on_event.call_count == 2
        emitted_event = mock_listener.on_event.call_args_list[1][0][0]
        assert isinstance(emitted_event, RealtimeModelAudioEvent)
        assert emitted_event.response_id == "resp_123"
        assert emitted_event.data == b"test audio"  # decoded from base64

        # Should update internal audio tracking state
        assert model._current_item_id == "item_456"

        # Test that audio state is tracked in the tracker
        audio_state = model._audio_state_tracker.get_state("item_456", 0)
        assert audio_state is not None
        assert audio_state.audio_length_ms > 0  # Should have some audio length

    @pytest.mark.asyncio
    async def test_backward_compat_output_item_added_and_done(self, model):
        """response.output_item.added/done paths emit item updates."""
        listener = AsyncMock()
        model.add_listener(listener)

        msg_added = {
            "type": "response.output_item.added",
            "item": {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "audio", "audio": "...", "transcript": "hi"},
                ],
            },
        }
        await model._handle_ws_event(msg_added)

        msg_done = {
            "type": "response.output_item.done",
            "item": {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "bye"}],
            },
        }
        await model._handle_ws_event(msg_done)

        # Ensure we emitted item_updated events for both cases
        types = [c[0][0].type for c in listener.on_event.call_args_list]
        assert types.count("item_updated") >= 2

    # Note: response.created/done require full OpenAI response payload which is
    # out-of-scope for unit tests here; covered indirectly via other branches.

    @pytest.mark.asyncio
    async def test_transcription_related_and_timeouts_and_speech_started(self, model, monkeypatch):
        listener = AsyncMock()
        model.add_listener(listener)

        # Prepare tracker state to simulate ongoing audio
        model._audio_state_tracker.set_audio_format("pcm16")
        model._audio_state_tracker.on_audio_delta("i1", 0, b"aaaa")
        model._ongoing_response = True

        # Patch sending to avoid websocket dependency
        monkeypatch.setattr(
            model,
            "_send_raw_message",
            AsyncMock(),
        )

        # Speech started should emit interrupted and cancel the response
        await model._handle_ws_event(
            {
                "type": "input_audio_buffer.speech_started",
                "event_id": "es1",
                "item_id": "i1",
                "audio_start_ms": 0,
                "audio_end_ms": 1,
            }
        )

        # Output transcript delta
        await model._handle_ws_event(
            {
                "type": "response.output_audio_transcript.delta",
                "event_id": "e3",
                "item_id": "i3",
                "response_id": "r3",
                "output_index": 0,
                "content_index": 0,
                "delta": "abc",
            }
        )

        # Timeout triggered
        await model._handle_ws_event(
            {
                "type": "input_audio_buffer.timeout_triggered",
                "event_id": "e4",
                "item_id": "i4",
                "audio_start_ms": 0,
                "audio_end_ms": 100,
            }
        )

        # raw + interrupted, raw + transcript delta, raw + timeout
        assert listener.on_event.call_count >= 6
        types = [call[0][0].type for call in listener.on_event.call_args_list]
        assert "audio_interrupted" in types
        assert "transcript_delta" in types
        assert "input_audio_timeout_triggered" in types


class TestSendEventAndConfig(TestOpenAIRealtimeWebSocketModel):
    @pytest.mark.asyncio
    async def test_send_event_dispatch(self, model, monkeypatch):
        send_raw = AsyncMock()
        monkeypatch.setattr(model, "_send_raw_message", send_raw)

        await model.send_event(RealtimeModelSendUserInput(user_input="hi"))
        await model.send_event(RealtimeModelSendAudio(audio=b"a", commit=False))
        await model.send_event(RealtimeModelSendAudio(audio=b"a", commit=True))
        await model.send_event(
            RealtimeModelSendToolOutput(
                tool_call=RealtimeModelToolCallEvent(name="t", call_id="c", arguments="{}"),
                output="ok",
                start_response=True,
            )
        )
        await model.send_event(RealtimeModelSendInterrupt())
        await model.send_event(RealtimeModelSendSessionUpdate(session_settings={"voice": "nova"}))

        # user_input -> 2 raw messages (item.create + response.create)
        # audio append -> 1, commit -> +1
        # tool output -> 1
        # interrupt -> 1
        # session update -> 1
        assert send_raw.await_count == 8

    def test_add_remove_listener_and_tools_conversion(self, model):
        listener = AsyncMock()
        model.add_listener(listener)
        model.add_listener(listener)
        assert len(model._listeners) == 1
        model.remove_listener(listener)
        assert len(model._listeners) == 0

        # tools conversion rejects non function tools and includes handoffs
        with pytest.raises(UserError):
            from agents.tool import Tool

            class X:
                name = "x"

            model._tools_to_session_tools(cast(list[Tool], [X()]), [])

        h = handoff(Agent(name="a"))
        out = model._tools_to_session_tools([], [h])
        assert out[0].name.startswith("transfer_to_")

    def test_get_and_update_session_config(self, model):
        settings = {
            "model_name": "gpt-realtime",
            "voice": "verse",
            "output_audio_format": "g711_ulaw",
            "modalities": ["audio"],
            "input_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        }
        cfg = model._get_session_config(settings)
        assert cfg.audio is not None and cfg.audio.output is not None
        assert cfg.audio.output.voice == "verse"

    @pytest.mark.asyncio
    async def test_handle_error_event_success(self, model):
        """Test successful handling of error events."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        error_event = {
            "type": "error",
            "event_id": "event_456",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "message": "Invalid API key provided",
            },
        }

        await model._handle_ws_event(error_event)

        # Should emit raw server event and error event to listeners
        assert mock_listener.on_event.call_count == 2
        emitted_event = mock_listener.on_event.call_args_list[1][0][0]
        assert isinstance(emitted_event, RealtimeModelErrorEvent)

    @pytest.mark.asyncio
    async def test_handle_tool_call_event_success(self, model):
        """Test successful handling of function call events."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        # Test response.output_item.done with function_call
        tool_call_event = {
            "type": "response.output_item.done",
            "event_id": "event_789",
            "response_id": "resp_789",
            "output_index": 0,
            "item": {
                "id": "call_123",
                "call_id": "call_123",
                "type": "function_call",
                "status": "completed",
                "name": "get_weather",
                "arguments": '{"location": "San Francisco"}',
            },
        }

        await model._handle_ws_event(tool_call_event)

        # Should emit raw server event, item updated, and tool call events
        assert mock_listener.on_event.call_count == 3

        # First should be raw server event, second should be item updated, third should be tool call
        calls = mock_listener.on_event.call_args_list
        tool_call_emitted = calls[2][0][0]
        assert isinstance(tool_call_emitted, RealtimeModelToolCallEvent)
        assert tool_call_emitted.name == "get_weather"
        assert tool_call_emitted.arguments == '{"location": "San Francisco"}'
        assert tool_call_emitted.call_id == "call_123"

    @pytest.mark.asyncio
    async def test_audio_timing_calculation_accuracy(self, model):
        """Test that audio timing calculations are accurate for interruption handling."""
        mock_listener = AsyncMock()
        model.add_listener(mock_listener)

        # Set up audio format on the tracker before testing
        model._audio_state_tracker.set_audio_format("pcm16")

        # Send multiple audio deltas to test cumulative timing
        audio_deltas = [
            {
                "type": "response.output_audio.delta",
                "event_id": "event_1",
                "response_id": "resp_1",
                "item_id": "item_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "dGVzdA==",  # 4 bytes -> "test"
            },
            {
                "type": "response.output_audio.delta",
                "event_id": "event_2",
                "response_id": "resp_1",
                "item_id": "item_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "bW9yZQ==",  # 4 bytes -> "more"
            },
        ]

        for event in audio_deltas:
            await model._handle_ws_event(event)

        # Should accumulate audio length: 8 bytes / 24 / 2 * 1000 = milliseconds
        # Total: 8 bytes / 24 / 2 * 1000
        expected_length = (8 / 24 / 2) * 1000

        # Test through the actual audio state tracker
        audio_state = model._audio_state_tracker.get_state("item_1", 0)
        assert audio_state is not None
        assert abs(audio_state.audio_length_ms - expected_length) < 0.001

    def test_calculate_audio_length_ms_pure_function(self, model):
        """Test the pure audio length calculation function."""
        from agents.realtime._util import calculate_audio_length_ms

        # Test various audio buffer sizes for pcm16 format
        assert calculate_audio_length_ms("pcm16", b"test") == (4 / 24 / 2) * 1000  # 4 bytes
        assert calculate_audio_length_ms("pcm16", b"") == 0  # empty
        assert calculate_audio_length_ms("pcm16", b"a" * 48) == 1000.0  # exactly 1000ms worth

        # Test g711 format
        assert calculate_audio_length_ms("g711_ulaw", b"test") == (4 / 8000) * 1000  # 4 bytes
        assert calculate_audio_length_ms("g711_alaw", b"a" * 8) == (8 / 8000) * 1000  # 8 bytes

    @pytest.mark.asyncio
    async def test_handle_audio_delta_state_management(self, model):
        """Test that _handle_audio_delta properly manages internal state."""
        # Set up audio format on the tracker before testing
        model._audio_state_tracker.set_audio_format("pcm16")

        # Create mock parsed event
        mock_parsed = Mock()
        mock_parsed.content_index = 5
        mock_parsed.item_id = "test_item"
        mock_parsed.delta = "dGVzdA=="  # "test" in base64
        mock_parsed.response_id = "resp_123"

        await model._handle_audio_delta(mock_parsed)

        # Check state was updated correctly
        assert model._current_item_id == "test_item"

        # Test that audio state is tracked correctly
        audio_state = model._audio_state_tracker.get_state("test_item", 5)
        assert audio_state is not None
        assert audio_state.audio_length_ms == (4 / 24 / 2) * 1000  # 4 bytes in milliseconds

        # Test that last audio item is tracked
        last_item = model._audio_state_tracker.get_last_audio_item()
        assert last_item == ("test_item", 5)
