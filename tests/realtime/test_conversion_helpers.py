import base64
from unittest.mock import Mock

from openai.types.beta.realtime.conversation_item import ConversationItem
from openai.types.beta.realtime.conversation_item_create_event import ConversationItemCreateEvent
from openai.types.beta.realtime.conversation_item_truncate_event import (
    ConversationItemTruncateEvent,
)
from openai.types.beta.realtime.input_audio_buffer_append_event import InputAudioBufferAppendEvent
from openai.types.beta.realtime.session_update_event import (
    SessionTracingTracingConfiguration,
)

from agents.realtime.config import RealtimeModelTracingConfig
from agents.realtime.model_inputs import (
    RealtimeModelSendAudio,
    RealtimeModelSendRawMessage,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
    RealtimeModelUserInputMessage,
)
from agents.realtime.openai_realtime import _ConversionHelper


class TestConversionHelperTryConvertRawMessage:
    """Test suite for _ConversionHelper.try_convert_raw_message method."""

    def test_try_convert_raw_message_valid_session_update(self):
        """Test converting a valid session.update raw message."""
        raw_message = RealtimeModelSendRawMessage(
            message={
                "type": "session.update",
                "other_data": {
                    "session": {
                        "modalities": ["text", "audio"],
                        "voice": "ash",
                    }
                },
            }
        )

        result = _ConversionHelper.try_convert_raw_message(raw_message)

        assert result is not None
        assert result.type == "session.update"

    def test_try_convert_raw_message_valid_response_create(self):
        """Test converting a valid response.create raw message."""
        raw_message = RealtimeModelSendRawMessage(
            message={
                "type": "response.create",
                "other_data": {},
            }
        )

        result = _ConversionHelper.try_convert_raw_message(raw_message)

        assert result is not None
        assert result.type == "response.create"

    def test_try_convert_raw_message_invalid_type(self):
        """Test converting an invalid message type returns None."""
        raw_message = RealtimeModelSendRawMessage(
            message={
                "type": "invalid.message.type",
                "other_data": {},
            }
        )

        result = _ConversionHelper.try_convert_raw_message(raw_message)

        assert result is None

    def test_try_convert_raw_message_malformed_data(self):
        """Test converting malformed message data returns None."""
        raw_message = RealtimeModelSendRawMessage(
            message={
                "type": "session.update",
                "other_data": {
                    "session": "invalid_session_data"  # Should be dict
                },
            }
        )

        result = _ConversionHelper.try_convert_raw_message(raw_message)

        assert result is None

    def test_try_convert_raw_message_missing_type(self):
        """Test converting message without type returns None."""
        raw_message = RealtimeModelSendRawMessage(
            message={
                "type": "missing.type.test",
                "other_data": {"some": "data"},
            }
        )

        result = _ConversionHelper.try_convert_raw_message(raw_message)

        assert result is None


class TestConversionHelperTracingConfig:
    """Test suite for _ConversionHelper.convert_tracing_config method."""

    def test_convert_tracing_config_none(self):
        """Test converting None tracing config."""
        result = _ConversionHelper.convert_tracing_config(None)
        assert result is None

    def test_convert_tracing_config_auto(self):
        """Test converting 'auto' tracing config."""
        result = _ConversionHelper.convert_tracing_config("auto")
        assert result == "auto"

    def test_convert_tracing_config_dict_full(self):
        """Test converting full tracing config dict."""
        tracing_config: RealtimeModelTracingConfig = {
            "group_id": "test-group",
            "metadata": {"env": "test"},
            "workflow_name": "test-workflow",
        }

        result = _ConversionHelper.convert_tracing_config(tracing_config)

        assert isinstance(result, SessionTracingTracingConfiguration)
        assert result.group_id == "test-group"
        assert result.metadata == {"env": "test"}
        assert result.workflow_name == "test-workflow"

    def test_convert_tracing_config_dict_partial(self):
        """Test converting partial tracing config dict."""
        tracing_config: RealtimeModelTracingConfig = {
            "group_id": "test-group",
        }

        result = _ConversionHelper.convert_tracing_config(tracing_config)

        assert isinstance(result, SessionTracingTracingConfiguration)
        assert result.group_id == "test-group"
        assert result.metadata is None
        assert result.workflow_name is None

    def test_convert_tracing_config_empty_dict(self):
        """Test converting empty tracing config dict."""
        tracing_config: RealtimeModelTracingConfig = {}

        result = _ConversionHelper.convert_tracing_config(tracing_config)

        assert isinstance(result, SessionTracingTracingConfiguration)
        assert result.group_id is None
        assert result.metadata is None
        assert result.workflow_name is None


class TestConversionHelperUserInput:
    """Test suite for _ConversionHelper user input conversion methods."""

    def test_convert_user_input_to_conversation_item_string(self):
        """Test converting string user input to conversation item."""
        event = RealtimeModelSendUserInput(user_input="Hello, world!")

        result = _ConversionHelper.convert_user_input_to_conversation_item(event)

        assert isinstance(result, ConversationItem)
        assert result.type == "message"
        assert result.role == "user"
        assert result.content is not None
        assert len(result.content) == 1
        assert result.content[0].type == "input_text"
        assert result.content[0].text == "Hello, world!"

    def test_convert_user_input_to_conversation_item_dict(self):
        """Test converting dict user input to conversation item."""
        user_input_dict: RealtimeModelUserInputMessage = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Hello"},
                {"type": "input_text", "text": "World"},
            ]
        }
        event = RealtimeModelSendUserInput(user_input=user_input_dict)

        result = _ConversionHelper.convert_user_input_to_conversation_item(event)

        assert isinstance(result, ConversationItem)
        assert result.type == "message"
        assert result.role == "user"
        assert result.content is not None
        assert len(result.content) == 2
        assert result.content[0].type == "input_text"
        assert result.content[0].text == "Hello"
        assert result.content[1].type == "input_text"
        assert result.content[1].text == "World"

    def test_convert_user_input_to_conversation_item_dict_empty_content(self):
        """Test converting dict user input with empty content."""
        user_input_dict: RealtimeModelUserInputMessage = {
            "type": "message",
            "role": "user",
            "content": []
        }
        event = RealtimeModelSendUserInput(user_input=user_input_dict)

        result = _ConversionHelper.convert_user_input_to_conversation_item(event)

        assert isinstance(result, ConversationItem)
        assert result.type == "message"
        assert result.role == "user"
        assert result.content is not None
        assert len(result.content) == 0

    def test_convert_user_input_to_item_create(self):
        """Test converting user input to item create event."""
        event = RealtimeModelSendUserInput(user_input="Test message")

        result = _ConversionHelper.convert_user_input_to_item_create(event)

        assert isinstance(result, ConversationItemCreateEvent)
        assert result.type == "conversation.item.create"
        assert isinstance(result.item, ConversationItem)
        assert result.item.type == "message"
        assert result.item.role == "user"


class TestConversionHelperAudio:
    """Test suite for _ConversionHelper.convert_audio_to_input_audio_buffer_append."""

    def test_convert_audio_to_input_audio_buffer_append(self):
        """Test converting audio data to input audio buffer append event."""
        audio_data = b"test audio data"
        event = RealtimeModelSendAudio(audio=audio_data, commit=False)

        result = _ConversionHelper.convert_audio_to_input_audio_buffer_append(event)

        assert isinstance(result, InputAudioBufferAppendEvent)
        assert result.type == "input_audio_buffer.append"

        # Verify base64 encoding
        expected_b64 = base64.b64encode(audio_data).decode("utf-8")
        assert result.audio == expected_b64

    def test_convert_audio_to_input_audio_buffer_append_empty(self):
        """Test converting empty audio data."""
        audio_data = b""
        event = RealtimeModelSendAudio(audio=audio_data, commit=True)

        result = _ConversionHelper.convert_audio_to_input_audio_buffer_append(event)

        assert isinstance(result, InputAudioBufferAppendEvent)
        assert result.type == "input_audio_buffer.append"
        assert result.audio == ""

    def test_convert_audio_to_input_audio_buffer_append_large_data(self):
        """Test converting large audio data."""
        audio_data = b"x" * 10000  # Large audio buffer
        event = RealtimeModelSendAudio(audio=audio_data, commit=False)

        result = _ConversionHelper.convert_audio_to_input_audio_buffer_append(event)

        assert isinstance(result, InputAudioBufferAppendEvent)
        assert result.type == "input_audio_buffer.append"

        # Verify it can be decoded back
        decoded = base64.b64decode(result.audio)
        assert decoded == audio_data


class TestConversionHelperToolOutput:
    """Test suite for _ConversionHelper.convert_tool_output method."""

    def test_convert_tool_output(self):
        """Test converting tool output to conversation item create event."""
        mock_tool_call = Mock()
        mock_tool_call.call_id = "call_123"

        event = RealtimeModelSendToolOutput(
            tool_call=mock_tool_call,
            output="Function executed successfully",
            start_response=False,
        )

        result = _ConversionHelper.convert_tool_output(event)

        assert isinstance(result, ConversationItemCreateEvent)
        assert result.type == "conversation.item.create"
        assert isinstance(result.item, ConversationItem)
        assert result.item.type == "function_call_output"
        assert result.item.output == "Function executed successfully"
        assert result.item.call_id == "call_123"

    def test_convert_tool_output_no_call_id(self):
        """Test converting tool output with None call_id."""
        mock_tool_call = Mock()
        mock_tool_call.call_id = None

        event = RealtimeModelSendToolOutput(
            tool_call=mock_tool_call,
            output="Output without call ID",
            start_response=False,
        )

        result = _ConversionHelper.convert_tool_output(event)

        assert isinstance(result, ConversationItemCreateEvent)
        assert result.type == "conversation.item.create"
        assert result.item.call_id is None

    def test_convert_tool_output_empty_output(self):
        """Test converting tool output with empty output."""
        mock_tool_call = Mock()
        mock_tool_call.call_id = "call_456"

        event = RealtimeModelSendToolOutput(
            tool_call=mock_tool_call,
            output="",
            start_response=True,
        )

        result = _ConversionHelper.convert_tool_output(event)

        assert isinstance(result, ConversationItemCreateEvent)
        assert result.item.output == ""
        assert result.item.call_id == "call_456"


class TestConversionHelperInterrupt:
    """Test suite for _ConversionHelper.convert_interrupt method."""

    def test_convert_interrupt(self):
        """Test converting interrupt parameters to conversation item truncate event."""
        current_item_id = "item_789"
        current_audio_content_index = 2
        elapsed_time_ms = 1500

        result = _ConversionHelper.convert_interrupt(
            current_item_id, current_audio_content_index, elapsed_time_ms
        )

        assert isinstance(result, ConversationItemTruncateEvent)
        assert result.type == "conversation.item.truncate"
        assert result.item_id == "item_789"
        assert result.content_index == 2
        assert result.audio_end_ms == 1500

    def test_convert_interrupt_zero_time(self):
        """Test converting interrupt with zero elapsed time."""
        result = _ConversionHelper.convert_interrupt("item_1", 0, 0)

        assert isinstance(result, ConversationItemTruncateEvent)
        assert result.type == "conversation.item.truncate"
        assert result.item_id == "item_1"
        assert result.content_index == 0
        assert result.audio_end_ms == 0

    def test_convert_interrupt_large_values(self):
        """Test converting interrupt with large values."""
        result = _ConversionHelper.convert_interrupt("item_xyz", 99, 999999)

        assert isinstance(result, ConversationItemTruncateEvent)
        assert result.type == "conversation.item.truncate"
        assert result.item_id == "item_xyz"
        assert result.content_index == 99
        assert result.audio_end_ms == 999999

    def test_convert_interrupt_empty_item_id(self):
        """Test converting interrupt with empty item ID."""
        result = _ConversionHelper.convert_interrupt("", 1, 100)

        assert isinstance(result, ConversationItemTruncateEvent)
        assert result.type == "conversation.item.truncate"
        assert result.item_id == ""
        assert result.content_index == 1
        assert result.audio_end_ms == 100
