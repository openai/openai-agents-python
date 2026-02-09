import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
import websockets

from agents import Agent
from agents.exceptions import UserError
from agents.handoffs import handoff
from agents.realtime.model import RealtimeModelConfig
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
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel, TransportConfig


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
    async def test_connect_with_call_id_and_model_raises_error(self, model):
        """Test that specifying both call_id and model raises UserError."""
        config = {
            "api_key": "test-api-key-123",
            "call_id": "call-123",
            "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
        }

        with pytest.raises(UserError, match="Cannot specify both `call_id` and `model_name`"):
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
    async def test_connect_with_empty_transport_config(self, mock_websocket):
        """Test that empty transport configuration works without error."""
        model = OpenAIRealtimeWebSocketModel(transport_config={})
        config: RealtimeModelConfig = {
            "api_key": "test-key",
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

                mock_connect.assert_called_once()
                kwargs = mock_connect.call_args.kwargs
                assert "ping_interval" not in kwargs
                assert "ping_timeout" not in kwargs
                assert "open_timeout" not in kwargs

    @pytest.mark.asyncio
    async def test_connect_already_connected_assertion(self, model, mock_websocket):
        """Test that connecting when already connected raises assertion error."""
        model._websocket = mock_websocket  # Simulate already connected

        config = {"api_key": "test-key"}

        with pytest.raises(AssertionError, match="Already connected"):
            await model.connect(config)

    @pytest.mark.asyncio
    async def test_session_update_disable_turn_detection(self, model, mock_websocket):
        """Session.update should allow users to disable turn-detection."""
        config = {
            "api_key": "test-api-key-123",
            "initial_model_settings": {
                "model_name": "gpt-4o-realtime-preview",
                "turn_detection": None,
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
        assert session["audio"]["input"]["turn_detection"] is None


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

    @pytest.mark.asyncio
    async def test_text_mode_output_item_content(self, model):
        """output_text content is properly handled in message items."""
        listener = AsyncMock()
        model.add_listener(listener)

        msg_added = {
            "type": "response.output_item.added",
            "item": {
                "id": "text_item_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "test data"},
                ],
            },
        }
        await model._handle_ws_event(msg_added)

        # Verify the item was updated with content
        assert listener.on_event.call_count >= 2
        item_updated_calls = [
            call for call in listener.on_event.call_args_list if call[0][0].type == "item_updated"
        ]
        assert len(item_updated_calls) >= 1

        item = item_updated_calls[0][0][0].item
        assert item.type == "message"
        assert item.role == "assistant"
        assert len(item.content) >= 1
        assert item.content[0].type == "text"
        assert item.content[0].text == "test data"

    # Note: response.created/done require full OpenAI response payload which is
    # out-of-scope for unit tests here; covered indirectly via other branches.

    @pytest.mark.asyncio
    async def test_transcription_related_and_timeouts_and_speech_started(self, model, monkeypatch):
        listener = AsyncMock()
        model.add_listener(listener)

        # Prepare tracker state to simulate ongoing audio
        model._audio_state_tracker.set_audio_format("pcm16")
        model._audio_state_tracker.on_audio_delta("i1", 0, b"a" * 96)

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

        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert truncate_events
        truncate_event = truncate_events[0]
        assert truncate_event.item_id == "i1"
        assert truncate_event.content_index == 0
        assert truncate_event.audio_end_ms == 1

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

    @pytest.mark.asyncio
    async def test_speech_started_skips_truncate_when_audio_complete(self, model, monkeypatch):
        model._audio_state_tracker.set_audio_format("pcm16")
        model._audio_state_tracker.on_audio_delta("i1", 0, b"a" * 48_000)
        state = model._audio_state_tracker.get_state("i1", 0)
        assert state is not None
        state.initial_received_time = datetime.now() - timedelta(seconds=5)

        monkeypatch.setattr(
            model,
            "_send_raw_message",
            AsyncMock(),
        )

        await model._handle_ws_event(
            {
                "type": "input_audio_buffer.speech_started",
                "event_id": "es2",
                "item_id": "i1",
                "audio_start_ms": 0,
                "audio_end_ms": 0,
            }
        )

        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert not truncate_events

    @pytest.mark.asyncio
    async def test_speech_started_truncates_when_response_ongoing(self, model, monkeypatch):
        model._audio_state_tracker.set_audio_format("pcm16")
        model._audio_state_tracker.on_audio_delta("i1", 0, b"a" * 48_000)
        state = model._audio_state_tracker.get_state("i1", 0)
        assert state is not None
        state.initial_received_time = datetime.now() - timedelta(seconds=5)
        model._ongoing_response = True

        monkeypatch.setattr(
            model,
            "_send_raw_message",
            AsyncMock(),
        )

        await model._handle_ws_event(
            {
                "type": "input_audio_buffer.speech_started",
                "event_id": "es3",
                "item_id": "i1",
                "audio_start_ms": 0,
                "audio_end_ms": 0,
            }
        )

        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert truncate_events
        assert truncate_events[0].audio_end_ms == 1000


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

    @pytest.mark.asyncio
    async def test_interrupt_force_cancel_overrides_auto_cancellation(self, model, monkeypatch):
        """Interrupt should send response.cancel even when auto cancel is enabled."""
        model._audio_state_tracker.set_audio_format("pcm16")
        model._audio_state_tracker.on_audio_delta("item_1", 0, b"\x00" * 4800)
        model._ongoing_response = True
        model._created_session = SimpleNamespace(
            audio=SimpleNamespace(
                input=SimpleNamespace(turn_detection=SimpleNamespace(interrupt_response=True))
            )
        )

        send_raw = AsyncMock()
        emit_event = AsyncMock()
        monkeypatch.setattr(model, "_send_raw_message", send_raw)
        monkeypatch.setattr(model, "_emit_event", emit_event)

        await model._send_interrupt(RealtimeModelSendInterrupt(force_response_cancel=True))

        assert send_raw.await_count == 2
        payload_types = {call.args[0].type for call in send_raw.call_args_list}
        assert payload_types == {"conversation.item.truncate", "response.cancel"}
        assert model._ongoing_response is False
        assert model._audio_state_tracker.get_last_audio_item() is None

    @pytest.mark.asyncio
    async def test_interrupt_respects_auto_cancellation_when_not_forced(self, model, monkeypatch):
        """Interrupt should avoid sending response.cancel when relying on automatic cancellation."""
        model._audio_state_tracker.set_audio_format("pcm16")
        model._audio_state_tracker.on_audio_delta("item_1", 0, b"\x00" * 4800)
        model._ongoing_response = True
        model._created_session = SimpleNamespace(
            audio=SimpleNamespace(
                input=SimpleNamespace(turn_detection=SimpleNamespace(interrupt_response=True))
            )
        )

        send_raw = AsyncMock()
        emit_event = AsyncMock()
        monkeypatch.setattr(model, "_send_raw_message", send_raw)
        monkeypatch.setattr(model, "_emit_event", emit_event)

        await model._send_interrupt(RealtimeModelSendInterrupt())

        assert send_raw.await_count == 1
        assert send_raw.call_args_list[0].args[0].type == "conversation.item.truncate"
        assert all(call.args[0].type != "response.cancel" for call in send_raw.call_args_list)
        assert model._ongoing_response is True

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

    def test_session_config_defaults_audio_formats_when_not_call(self, model):
        settings: dict[str, Any] = {}
        cfg = model._get_session_config(settings)
        assert cfg.audio is not None
        assert cfg.audio.input is not None
        assert cfg.audio.input.format is not None
        assert cfg.audio.input.format.type == "audio/pcm"
        assert cfg.audio.output is not None
        assert cfg.audio.output.format is not None
        assert cfg.audio.output.format.type == "audio/pcm"

    def test_session_config_preserves_sip_audio_formats(self, model):
        model._call_id = "call-123"
        settings = {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        }
        cfg = model._get_session_config(settings)
        assert cfg.audio is not None
        assert cfg.audio.input is not None
        assert cfg.audio.input.format is None
        assert cfg.audio.output is not None
        assert cfg.audio.output.format is None

    def test_session_config_respects_audio_block_and_output_modalities(self, model):
        settings = {
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "modalities": ["audio"],
            "output_modalities": ["text"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "createResponse": True,
                        "silenceDurationMs": 450,
                        "modelVersion": "default",
                    },
                },
                "output": {
                    "format": {"type": "audio/pcma"},
                    "voice": "synth-1",
                    "speed": 1.5,
                },
            },
        }
        cfg = model._get_session_config(settings)

        assert cfg.output_modalities == ["text"]
        assert cfg.audio is not None
        assert cfg.audio.input.format is not None
        assert cfg.audio.input.format.type == "audio/pcmu"
        assert cfg.audio.output.format is not None
        assert cfg.audio.output.format.type == "audio/pcma"
        assert cfg.audio.output.voice == "synth-1"
        assert cfg.audio.output.speed == 1.5
        assert cfg.audio.input.transcription is not None

        turn_detection = cfg.audio.input.turn_detection
        turn_detection_mapping = (
            turn_detection if isinstance(turn_detection, dict) else turn_detection.model_dump()
        )
        assert turn_detection_mapping["create_response"] is True
        assert turn_detection_mapping["silence_duration_ms"] == 450
        assert turn_detection_mapping["model_version"] == "default"
        assert "silenceDurationMs" not in turn_detection_mapping
        assert "modelVersion" not in turn_detection_mapping

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

        # Should accumulate audio length: 8 bytes -> 4 samples -> (4 / 24000) * 1000 ≈ 0.167 ms
        expected_length = (8 / (24_000 * 2)) * 1000

        # Test through the actual audio state tracker
        audio_state = model._audio_state_tracker.get_state("item_1", 0)
        assert audio_state is not None
        assert audio_state.audio_length_ms == pytest.approx(expected_length, rel=0, abs=1e-6)

    def test_calculate_audio_length_ms_pure_function(self, model):
        """Test the pure audio length calculation function."""
        from agents.realtime._util import calculate_audio_length_ms

        # Test various audio buffer sizes for pcm16 format
        expected_pcm = (len(b"test") / (24_000 * 2)) * 1000
        assert calculate_audio_length_ms("pcm16", b"test") == pytest.approx(
            expected_pcm, rel=0, abs=1e-6
        )  # 4 bytes
        assert calculate_audio_length_ms("pcm16", b"") == 0  # empty
        assert calculate_audio_length_ms("pcm16", b"a" * 48) == pytest.approx(
            (48 / (24_000 * 2)) * 1000, rel=0, abs=1e-6
        )  # exactly 1ms worth

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
        expected_ms = (len(b"test") / (24_000 * 2)) * 1000
        assert audio_state.audio_length_ms == pytest.approx(expected_ms, rel=0, abs=1e-6)

        # Test that last audio item is tracked
        last_item = model._audio_state_tracker.get_last_audio_item()
        assert last_item == ("test_item", 5)


class TestTransportIntegration:
    """Integration tests for transport configuration using a local WebSocket server."""

    @pytest.mark.asyncio
    async def test_connect_to_local_server(self):
        """Test connecting to a real local server with transport config."""
        received_messages = []
        import asyncio

        async def handler(websocket):
            try:
                # Use async iteration for compatibility with newer websockets
                async for message in websocket:
                    received_messages.append(json.loads(message))
                    # Respond to session update
                    # We need to provide a minimally valid session object
                    response = {
                        "type": "session.updated",
                        "event_id": "event_123",
                        "session": {
                            "id": "sess_001",
                            "object": "realtime.session",
                            "model": "gpt-4o-realtime-preview",
                            "modalities": ["audio", "text"],
                            "instructions": "",
                            "voice": "alloy",
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "input_audio_transcription": None,
                            "turn_detection": None,
                            "tools": [],
                            "tool_choice": "auto",
                            "temperature": 0.8,
                            "max_response_output_tokens": "inf",
                        },
                    }
                    await websocket.send(json.dumps(response))
            except Exception:
                pass

        # Create a model instance
        model = OpenAIRealtimeWebSocketModel()

        # Start a local server
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            # Get the assigned port
            assert server.sockets

            # Cast sockets to list to make mypy happy as Iterable isn't indexable directly
            sockets = list(server.sockets)
            port = sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/v1/realtime"

            # Connect with transport config
            transport: TransportConfig = {
                "ping_interval": 0.5,
                "ping_timeout": 0.5,
                "handshake_timeout": 1.0,
            }

            model = OpenAIRealtimeWebSocketModel(transport_config=transport)
            config: RealtimeModelConfig = {
                "api_key": "test-key",
                "url": url,
                "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
            }

            await model.connect(config)

            # Wait briefly for the connection logic to send session.update
            # In a real scenario we'd wait for an event, but here we just sleep briefly
            await asyncio.sleep(0.2)

            # Verify we are connected
            assert model._websocket is not None

            # Verify the server received the session.update message
            assert len(received_messages) > 0
            session_update = next(
                (m for m in received_messages if m["type"] == "session.update"), None
            )
            assert session_update is not None

            # Clean up
            await model.close()
            assert model._websocket is None

    @pytest.mark.asyncio
    async def test_ping_timeout_success_when_server_responds_quickly(self):
        """Test that connection stays alive when server responds to pings within timeout."""

        async def responsive_handler(websocket):
            # Server that responds normally - websockets library handles ping/pong automatically
            async for _ in websocket:
                pass

        model = OpenAIRealtimeWebSocketModel()

        async with websockets.serve(responsive_handler, "127.0.0.1", 0) as server:
            sockets = list(server.sockets)
            port = sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/v1/realtime"

            # Client with reasonable ping settings - server responds quickly so this should work
            transport: TransportConfig = {
                "ping_interval": 0.1,  # Send ping every 100ms
                "ping_timeout": 1.0,  # Allow 1 second for pong response (generous)
            }
            model = OpenAIRealtimeWebSocketModel(transport_config=transport)
            config: RealtimeModelConfig = {
                "api_key": "test-key",
                "url": url,
                "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
            }

            await model.connect(config)

            # Wait for multiple ping/pong cycles
            await asyncio.sleep(0.4)

            # Connection should still be open
            assert model._websocket is not None
            assert model._websocket.close_code is None

            await model.close()

    @pytest.mark.asyncio
    async def test_ping_timeout_config_is_applied(self):
        """Test that ping_timeout configuration is properly applied to connection.

        This test verifies the ping_timeout parameter is passed to the websocket
        connection. Since the websockets library handles pong responses automatically,
        we verify the configuration is applied rather than testing actual timeout behavior.
        """
        from unittest.mock import AsyncMock, patch

        # Track what parameters were passed to websockets.connect
        captured_kwargs_short: dict[str, Any] = {}
        captured_kwargs_long: dict[str, Any] = {}

        async def capture_connect_short(*args, **kwargs):
            captured_kwargs_short.update(kwargs)
            mock_ws = AsyncMock()
            mock_ws.close_code = None
            return mock_ws

        async def capture_connect_long(*args, **kwargs):
            captured_kwargs_long.update(kwargs)
            mock_ws = AsyncMock()
            mock_ws.close_code = None
            return mock_ws

        # Test with short ping_timeout
        transport_short: TransportConfig = {
            "ping_interval": 0.1,
            "ping_timeout": 0.05,  # Very short timeout
        }
        model_short = OpenAIRealtimeWebSocketModel(transport_config=transport_short)
        with patch("websockets.connect", side_effect=capture_connect_short):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                config_short: RealtimeModelConfig = {
                    "api_key": "test-key",
                    "url": "ws://localhost:8080/v1/realtime",
                    "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
                }
                await model_short.connect(config_short)

        assert captured_kwargs_short.get("ping_interval") == 0.1
        assert captured_kwargs_short.get("ping_timeout") == 0.05

        # Test with longer ping_timeout (use a fresh model)
        transport_long: TransportConfig = {
            "ping_interval": 5.0,
            "ping_timeout": 10.0,  # Longer timeout
        }
        model_long = OpenAIRealtimeWebSocketModel(transport_config=transport_long)
        with patch("websockets.connect", side_effect=capture_connect_long):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                config_long: RealtimeModelConfig = {
                    "api_key": "test-key",
                    "url": "ws://localhost:8080/v1/realtime",
                    "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
                }
                await model_long.connect(config_long)

        assert captured_kwargs_long.get("ping_interval") == 5.0
        assert captured_kwargs_long.get("ping_timeout") == 10.0

    @pytest.mark.asyncio
    async def test_ping_timeout_disabled_vs_enabled(self):
        """Test that ping timeout can be disabled (None) vs enabled with a value."""
        from unittest.mock import AsyncMock, patch

        captured_kwargs_disabled: dict[str, Any] = {}
        captured_kwargs_enabled: dict[str, Any] = {}

        async def capture_connect_disabled(*args, **kwargs):
            captured_kwargs_disabled.update(kwargs)
            mock_ws = AsyncMock()
            mock_ws.close_code = None
            return mock_ws

        async def capture_connect_enabled(*args, **kwargs):
            captured_kwargs_enabled.update(kwargs)
            mock_ws = AsyncMock()
            mock_ws.close_code = None
            return mock_ws

        # Test with ping disabled
        transport_disabled: TransportConfig = {
            "ping_interval": None,  # Disable pings entirely
            "ping_timeout": None,
        }
        model_disabled = OpenAIRealtimeWebSocketModel(transport_config=transport_disabled)
        with patch("websockets.connect", side_effect=capture_connect_disabled):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                config_disabled: RealtimeModelConfig = {
                    "api_key": "test-key",
                    "url": "ws://localhost:8080/v1/realtime",
                    "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
                }
                await model_disabled.connect(config_disabled)

        assert captured_kwargs_disabled.get("ping_interval") is None
        assert captured_kwargs_disabled.get("ping_timeout") is None

        # Test with ping enabled (use a fresh model)
        transport_enabled: TransportConfig = {
            "ping_interval": 1.0,
            "ping_timeout": 2.0,
        }
        model_enabled = OpenAIRealtimeWebSocketModel(transport_config=transport_enabled)
        with patch("websockets.connect", side_effect=capture_connect_enabled):
            with patch("asyncio.create_task") as mock_create_task:
                mock_task = AsyncMock()

                def mock_create_task_func(coro):
                    coro.close()
                    return mock_task

                mock_create_task.side_effect = mock_create_task_func

                config_enabled: RealtimeModelConfig = {
                    "api_key": "test-key",
                    "url": "ws://localhost:8080/v1/realtime",
                    "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
                }
                await model_enabled.connect(config_enabled)

        assert captured_kwargs_enabled.get("ping_interval") == 1.0
        assert captured_kwargs_enabled.get("ping_timeout") == 2.0

    @pytest.mark.asyncio
    async def test_handshake_timeout_success_when_server_responds_quickly(self):
        """Test that connection succeeds when server responds within timeout."""

        async def quick_handler(websocket):
            # Server that accepts connections immediately
            async for _ in websocket:
                pass

        model = OpenAIRealtimeWebSocketModel()

        async with websockets.serve(quick_handler, "127.0.0.1", 0) as server:
            sockets = list(server.sockets)
            port = sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/v1/realtime"

            # Client with generous handshake timeout - server is fast so this should work
            transport: TransportConfig = {
                "handshake_timeout": 5.0,  # 5 seconds is plenty for local connection
            }
            model = OpenAIRealtimeWebSocketModel(transport_config=transport)
            config: RealtimeModelConfig = {
                "api_key": "test-key",
                "url": url,
                "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
            }

            await model.connect(config)

            # Should connect successfully
            assert model._websocket is not None
            assert model._websocket.close_code is None

            await model.close()

    @pytest.mark.asyncio
    async def test_handshake_timeout_with_delayed_server(self):
        """Test handshake timeout behavior with a server that has a defined handshake delay.

        Uses the same server with a fixed delay threshold to test both:
        - Success: client timeout > server delay
        - Failure: client timeout < server delay
        """
        import base64
        import hashlib

        # Server handshake delay threshold (in seconds)
        SERVER_HANDSHAKE_DELAY = 0.3

        shutdown_event = asyncio.Event()
        connections_attempted = []

        async def delayed_websocket_server(reader, writer):
            """A WebSocket server that delays the handshake by a fixed amount."""
            connections_attempted.append(True)
            try:
                # Read HTTP upgrade request
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=5.0)
                    if not chunk:
                        return
                    request += chunk

                # Extract Sec-WebSocket-Key
                key = None
                for line in request.decode().split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                        break

                if not key:
                    writer.close()
                    return

                # Intentional delay before completing handshake
                await asyncio.sleep(SERVER_HANDSHAKE_DELAY)

                # Generate accept key
                GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()

                # Send HTTP 101 Switching Protocols response
                response = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                )
                writer.write(response.encode())
                await writer.drain()

                # Keep connection open until shutdown
                await shutdown_event.wait()

            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(delayed_websocket_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/v1/realtime"

        try:
            # Test 1: FAILURE - Client timeout < server delay
            # Client gives up before server completes handshake
            transport_fail: TransportConfig = {
                "handshake_timeout": 0.1,  # 100ms < 300ms server delay → will timeout
            }
            model_fail = OpenAIRealtimeWebSocketModel(transport_config=transport_fail)
            config_fail: RealtimeModelConfig = {
                "api_key": "test-key",
                "url": url,
                "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
            }

            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await model_fail.connect(config_fail)

            # Verify connection was attempted
            assert len(connections_attempted) >= 1

            # Test 2: SUCCESS - Client timeout > server delay
            # Client waits long enough for server to complete handshake
            transport_success: TransportConfig = {
                "handshake_timeout": 1.0,  # 1000ms > 300ms server delay → will succeed
            }
            model_success = OpenAIRealtimeWebSocketModel(transport_config=transport_success)
            config_success: RealtimeModelConfig = {
                "api_key": "test-key",
                "url": url,
                "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
            }

            await model_success.connect(config_success)

            # Verify successful connection
            assert model_success._websocket is not None
            assert model_success._websocket.close_code is None

            await model_success.close()

        finally:
            shutdown_event.set()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_ping_interval_comparison_fast_vs_slow(self):
        """Test that faster ping intervals detect issues sooner than slower ones."""

        connection_durations: dict[str, float] = {}

        async def handler(websocket):
            # Simple handler that stays connected
            async for _ in websocket:
                pass

        async def test_with_ping_interval(interval: float, label: str):
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                sockets = list(server.sockets)
                port = sockets[0].getsockname()[1]
                url = f"ws://127.0.0.1:{port}/v1/realtime"

                transport: TransportConfig = {
                    "ping_interval": interval,
                    "ping_timeout": 2.0,  # Same timeout for both
                }
                model = OpenAIRealtimeWebSocketModel(transport_config=transport)
                config: RealtimeModelConfig = {
                    "api_key": "test-key",
                    "url": url,
                    "initial_model_settings": {"model_name": "gpt-4o-realtime-preview"},
                }

                start = asyncio.get_event_loop().time()
                await model.connect(config)

                # Let it run for a bit
                await asyncio.sleep(0.3)

                end = asyncio.get_event_loop().time()
                connection_durations[label] = end - start

                # Both should stay connected with valid server
                assert model._websocket is not None
                assert model._websocket.close_code is None

                await model.close()

        # Test with fast ping interval
        await test_with_ping_interval(0.05, "fast")

        # Test with slow ping interval
        await test_with_ping_interval(0.5, "slow")

        # Both should have completed successfully
        assert "fast" in connection_durations
        assert "slow" in connection_durations
