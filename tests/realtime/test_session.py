from typing import cast
from unittest.mock import AsyncMock, Mock, PropertyMock

import pytest

from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail
from agents.handoffs import Handoff
from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig
from agents.realtime.events import (
    RealtimeAgentEndEvent,
    RealtimeAgentStartEvent,
    RealtimeAudio,
    RealtimeAudioEnd,
    RealtimeAudioInterrupted,
    RealtimeError,
    RealtimeGuardrailTripped,
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
    RealtimeRawModelEvent,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from agents.realtime.items import (
    AssistantMessageItem,
    AssistantText,
    InputAudio,
    InputText,
    RealtimeItem,
    UserMessageItem,
)
from agents.realtime.model import RealtimeModel
from agents.realtime.model_events import (
    RealtimeModelAudioDoneEvent,
    RealtimeModelAudioEvent,
    RealtimeModelAudioInterruptedEvent,
    RealtimeModelConnectionStatusEvent,
    RealtimeModelErrorEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelOtherEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelTurnStartedEvent,
)
from agents.realtime.session import RealtimeSession
from agents.tool import FunctionTool
from agents.tool_context import ToolContext


class MockRealtimeModel(RealtimeModel):
    def __init__(self):
        super().__init__()
        self.listeners = []
        self.connect_called = False
        self.close_called = False
        self.sent_events = []
        # Legacy tracking for tests that haven't been updated yet
        self.sent_messages = []
        self.sent_audio = []
        self.sent_tool_outputs = []
        self.interrupts_called = 0

    async def connect(self, options=None):
        self.connect_called = True

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def send_event(self, event):
        from agents.realtime.model_inputs import (
            RealtimeModelSendAudio,
            RealtimeModelSendInterrupt,
            RealtimeModelSendToolOutput,
            RealtimeModelSendUserInput,
        )

        self.sent_events.append(event)

        # Update legacy tracking for compatibility
        if isinstance(event, RealtimeModelSendUserInput):
            self.sent_messages.append(event.user_input)
        elif isinstance(event, RealtimeModelSendAudio):
            self.sent_audio.append((event.audio, event.commit))
        elif isinstance(event, RealtimeModelSendToolOutput):
            self.sent_tool_outputs.append((event.tool_call, event.output, event.start_response))
        elif isinstance(event, RealtimeModelSendInterrupt):
            self.interrupts_called += 1

    async def close(self):
        self.close_called = True


@pytest.fixture
def mock_agent():
    agent = Mock(spec=RealtimeAgent)
    agent.get_all_tools = AsyncMock(return_value=[])

    type(agent).handoffs = PropertyMock(return_value=[])
    return agent


@pytest.fixture
def mock_model():
    return MockRealtimeModel()


@pytest.fixture
def mock_function_tool():
    tool = Mock(spec=FunctionTool)
    tool.name = "test_function"
    tool.on_invoke_tool = AsyncMock(return_value="function_result")
    return tool


@pytest.fixture
def mock_handoff():
    handoff = Mock(spec=Handoff)
    handoff.name = "test_handoff"
    return handoff


class TestEventHandling:
    """Test suite for event handling and transformation in RealtimeSession.on_event"""

    @pytest.mark.asyncio
    async def test_error_event_transformation(self, mock_model, mock_agent):
        """Test that error events are properly transformed and queued"""
        session = RealtimeSession(mock_model, mock_agent, None)

        error_event = RealtimeModelErrorEvent(error="Test error")

        await session.on_event(error_event)

        # Check that events were queued
        assert session._event_queue.qsize() == 2

        # First event should be raw model event
        raw_event = await session._event_queue.get()
        assert isinstance(raw_event, RealtimeRawModelEvent)
        assert raw_event.data == error_event

        # Second event should be transformed error event
        error_session_event = await session._event_queue.get()
        assert isinstance(error_session_event, RealtimeError)
        assert error_session_event.error == "Test error"

    @pytest.mark.asyncio
    async def test_audio_events_transformation(self, mock_model, mock_agent):
        """Test that audio-related events are properly transformed"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Test audio event
        audio_event = RealtimeModelAudioEvent(data=b"audio_data", response_id="resp_1")
        await session.on_event(audio_event)

        # Test audio interrupted event
        interrupted_event = RealtimeModelAudioInterruptedEvent()
        await session.on_event(interrupted_event)

        # Test audio done event
        done_event = RealtimeModelAudioDoneEvent()
        await session.on_event(done_event)

        # Should have 6 events total (2 per event: raw + transformed)
        assert session._event_queue.qsize() == 6

        # Check audio event transformation
        await session._event_queue.get()  # raw event
        audio_session_event = await session._event_queue.get()
        assert isinstance(audio_session_event, RealtimeAudio)
        assert audio_session_event.audio == audio_event

        # Check audio interrupted transformation
        await session._event_queue.get()  # raw event
        interrupted_session_event = await session._event_queue.get()
        assert isinstance(interrupted_session_event, RealtimeAudioInterrupted)

        # Check audio done transformation
        await session._event_queue.get()  # raw event
        done_session_event = await session._event_queue.get()
        assert isinstance(done_session_event, RealtimeAudioEnd)

    @pytest.mark.asyncio
    async def test_turn_events_transformation(self, mock_model, mock_agent):
        """Test that turn start/end events are properly transformed"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Test turn started event
        turn_started = RealtimeModelTurnStartedEvent()
        await session.on_event(turn_started)

        # Test turn ended event
        turn_ended = RealtimeModelTurnEndedEvent()
        await session.on_event(turn_ended)

        # Should have 4 events total (2 per event: raw + transformed)
        assert session._event_queue.qsize() == 4

        # Check turn started transformation
        await session._event_queue.get()  # raw event
        start_session_event = await session._event_queue.get()
        assert isinstance(start_session_event, RealtimeAgentStartEvent)
        assert start_session_event.agent == mock_agent

        # Check turn ended transformation
        await session._event_queue.get()  # raw event
        end_session_event = await session._event_queue.get()
        assert isinstance(end_session_event, RealtimeAgentEndEvent)
        assert end_session_event.agent == mock_agent

    @pytest.mark.asyncio
    async def test_transcription_completed_event_updates_history(self, mock_model, mock_agent):
        """Test that transcription completed events update history and emit events"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Set up initial history with an audio message
        initial_item = UserMessageItem(
            item_id="item_1", role="user", content=[InputAudio(transcript=None)]
        )
        session._history = [initial_item]

        # Create transcription completed event
        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_1", transcript="Hello world"
        )

        await session.on_event(transcription_event)

        # Check that history was updated
        assert len(session._history) == 1
        updated_item = session._history[0]
        assert updated_item.content[0].transcript == "Hello world"  # type: ignore
        assert updated_item.status == "completed"  # type: ignore

        # Should have 2 events: raw + history updated
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)
        assert len(history_event.history) == 1

    @pytest.mark.asyncio
    async def test_item_updated_event_adds_new_item(self, mock_model, mock_agent):
        """Test that item_updated events add new items to history"""
        session = RealtimeSession(mock_model, mock_agent, None)

        new_item = AssistantMessageItem(
            item_id="new_item", role="assistant", content=[AssistantText(text="Hello")]
        )

        item_updated_event = RealtimeModelItemUpdatedEvent(item=new_item)

        await session.on_event(item_updated_event)

        # Check that item was added to history
        assert len(session._history) == 1
        assert session._history[0] == new_item

        # Should have 2 events: raw + history added
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryAdded)
        assert history_event.item == new_item

    @pytest.mark.asyncio
    async def test_item_updated_event_updates_existing_item(self, mock_model, mock_agent):
        """Test that item_updated events update existing items in history"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Set up initial history
        initial_item = AssistantMessageItem(
            item_id="existing_item", role="assistant", content=[AssistantText(text="Initial")]
        )
        session._history = [initial_item]

        # Create updated version
        updated_item = AssistantMessageItem(
            item_id="existing_item", role="assistant", content=[AssistantText(text="Updated")]
        )

        item_updated_event = RealtimeModelItemUpdatedEvent(item=updated_item)

        await session.on_event(item_updated_event)

        # Check that item was updated
        assert len(session._history) == 1
        updated_item = cast(AssistantMessageItem, session._history[0])
        assert updated_item.content[0].text == "Updated"  # type: ignore

        # Should have 2 events: raw + history updated (not added)
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)

    @pytest.mark.asyncio
    async def test_item_deleted_event_removes_item(self, mock_model, mock_agent):
        """Test that item_deleted events remove items from history"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Set up initial history with multiple items
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        item2 = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Second")]
        )
        session._history = [item1, item2]

        # Delete first item
        delete_event = RealtimeModelItemDeletedEvent(item_id="item_1")

        await session.on_event(delete_event)

        # Check that item was removed
        assert len(session._history) == 1
        assert session._history[0].item_id == "item_2"

        # Should have 2 events: raw + history updated
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)
        assert len(history_event.history) == 1

    @pytest.mark.asyncio
    async def test_ignored_events_only_generate_raw_events(self, mock_model, mock_agent):
        """Test that ignored events (transcript_delta, connection_status, other) only generate raw
        events"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Test transcript delta (should be ignored per TODO comment)
        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="hello", response_id="resp_1"
        )
        await session.on_event(transcript_event)

        # Test connection status (should be ignored)
        connection_event = RealtimeModelConnectionStatusEvent(status="connected")
        await session.on_event(connection_event)

        # Test other event (should be ignored)
        other_event = RealtimeModelOtherEvent(data={"custom": "data"})
        await session.on_event(other_event)

        # Should only have 3 raw events (no transformed events)
        assert session._event_queue.qsize() == 3

        for _ in range(3):
            event = await session._event_queue.get()
            assert isinstance(event, RealtimeRawModelEvent)

    @pytest.mark.asyncio
    async def test_function_call_event_triggers_tool_handling(self, mock_model, mock_agent):
        """Test that function_call events trigger tool call handling"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Create function call event
        function_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_123", arguments='{"param": "value"}'
        )

        # We'll test the detailed tool handling in a separate test class
        # Here we just verify that it gets to the handler
        with pytest.MonkeyPatch().context() as m:
            handle_tool_call_mock = AsyncMock()
            m.setattr(session, "_handle_tool_call", handle_tool_call_mock)

            await session.on_event(function_call_event)

            # Should have called the tool handler
            handle_tool_call_mock.assert_called_once_with(function_call_event)

            # Should still have raw event
            assert session._event_queue.qsize() == 1
            raw_event = await session._event_queue.get()
            assert isinstance(raw_event, RealtimeRawModelEvent)
            assert raw_event.data == function_call_event


class TestHistoryManagement:
    """Test suite for history management and audio transcription in
    RealtimeSession._get_new_history"""

    def test_merge_transcript_into_existing_audio_message(self):
        """Test merging audio transcript into existing placeholder input_audio message"""
        # Create initial history with audio message without transcript
        initial_item = UserMessageItem(
            item_id="item_1",
            role="user",
            content=[
                InputText(text="Before audio"),
                InputAudio(transcript=None, audio="audio_data"),
                InputText(text="After audio"),
            ],
        )
        old_history = [initial_item]

        # Create transcription completed event
        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_1", transcript="Hello world"
        )

        # Apply the history update
        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), transcription_event
        )

        # Verify the transcript was merged
        assert len(new_history) == 1
        updated_item = cast(UserMessageItem, new_history[0])
        assert updated_item.item_id == "item_1"
        assert hasattr(updated_item, "status") and updated_item.status == "completed"
        assert len(updated_item.content) == 3

        # Check that audio content got transcript but other content unchanged
        assert cast(InputText, updated_item.content[0]).text == "Before audio"
        assert cast(InputAudio, updated_item.content[1]).transcript == "Hello world"
        # Should preserve audio data
        assert cast(InputAudio, updated_item.content[1]).audio == "audio_data"
        assert cast(InputText, updated_item.content[2]).text == "After audio"

    def test_merge_transcript_preserves_other_items(self):
        """Test that merging transcript preserves other items in history"""
        # Create history with multiple items
        item1 = UserMessageItem(
            item_id="item_1", role="user", content=[InputText(text="First message")]
        )
        item2 = UserMessageItem(
            item_id="item_2", role="user", content=[InputAudio(transcript=None)]
        )
        item3 = AssistantMessageItem(
            item_id="item_3", role="assistant", content=[AssistantText(text="Third message")]
        )
        old_history = [item1, item2, item3]

        # Create transcription event for item_2
        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_2", transcript="Transcribed audio"
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), transcription_event
        )

        # Should have same number of items
        assert len(new_history) == 3

        # First and third items should be unchanged
        assert new_history[0] == item1
        assert new_history[2] == item3

        # Second item should have transcript
        updated_item2 = cast(UserMessageItem, new_history[1])
        assert updated_item2.item_id == "item_2"
        assert cast(InputAudio, updated_item2.content[0]).transcript == "Transcribed audio"
        assert hasattr(updated_item2, "status") and updated_item2.status == "completed"

    def test_merge_transcript_only_affects_matching_audio_content(self):
        """Test that transcript merge only affects audio content, not text content"""
        # Create item with mixed content including multiple audio items
        item = UserMessageItem(
            item_id="item_1",
            role="user",
            content=[
                InputText(text="Text content"),
                InputAudio(transcript=None, audio="audio1"),
                InputAudio(transcript="existing", audio="audio2"),
                InputText(text="More text"),
            ],
        )
        old_history = [item]

        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_1", transcript="New transcript"
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), transcription_event
        )

        updated_item = cast(UserMessageItem, new_history[0])

        # Text content should be unchanged
        assert cast(InputText, updated_item.content[0]).text == "Text content"
        assert cast(InputText, updated_item.content[3]).text == "More text"

        # All audio content should have the new transcript (current implementation overwrites all)
        assert cast(InputAudio, updated_item.content[1]).transcript == "New transcript"
        assert (
            cast(InputAudio, updated_item.content[2]).transcript == "New transcript"
        )  # Implementation overwrites existing

    def test_update_existing_item_by_id(self):
        """Test updating an existing item by item_id"""
        # Create initial history
        original_item = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="Original")]
        )
        old_history = [original_item]

        # Create updated version of same item
        updated_item = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="Updated")]
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), updated_item
        )

        # Should have same number of items
        assert len(new_history) == 1

        # Item should be updated
        result_item = cast(AssistantMessageItem, new_history[0])
        assert result_item.item_id == "item_1"
        assert result_item.content[0].text == "Updated"  # type: ignore

    def test_update_existing_item_preserves_order(self):
        """Test that updating existing item preserves its position in history"""
        # Create history with multiple items
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        item2 = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Second")]
        )
        item3 = AssistantMessageItem(
            item_id="item_3", role="assistant", content=[AssistantText(text="Third")]
        )
        old_history = [item1, item2, item3]

        # Update middle item
        updated_item2 = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Updated Second")]
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), updated_item2
        )

        # Should have same number of items in same order
        assert len(new_history) == 3
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"
        assert new_history[2].item_id == "item_3"

        # Middle item should be updated
        updated_result = cast(AssistantMessageItem, new_history[1])
        assert updated_result.content[0].text == "Updated Second"  # type: ignore

        # Other items should be unchanged
        item1_result = cast(AssistantMessageItem, new_history[0])
        item3_result = cast(AssistantMessageItem, new_history[2])
        assert item1_result.content[0].text == "First"  # type: ignore
        assert item3_result.content[0].text == "Third"  # type: ignore

    def test_insert_new_item_after_previous_item(self):
        """Test inserting new item after specified previous_item_id"""
        # Create initial history
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        item3 = AssistantMessageItem(
            item_id="item_3", role="assistant", content=[AssistantText(text="Third")]
        )
        old_history = [item1, item3]

        # Create new item to insert between them
        new_item = AssistantMessageItem(
            item_id="item_2",
            previous_item_id="item_1",
            role="assistant",
            content=[AssistantText(text="Second")],
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), new_item
        )

        # Should have one more item
        assert len(new_history) == 3

        # Items should be in correct order
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"
        assert new_history[2].item_id == "item_3"

        # Content should be correct
        item2_result = cast(AssistantMessageItem, new_history[1])
        assert item2_result.content[0].text == "Second"  # type: ignore

    def test_insert_new_item_after_nonexistent_previous_item(self):
        """Test that item with nonexistent previous_item_id gets added to end"""
        # Create initial history
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        old_history = [item1]

        # Create new item with nonexistent previous_item_id
        new_item = AssistantMessageItem(
            item_id="item_2",
            previous_item_id="nonexistent",
            role="assistant",
            content=[AssistantText(text="Second")],
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), new_item
        )

        # Should add to end when previous_item_id not found
        assert len(new_history) == 2
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"

    def test_add_new_item_to_end_when_no_previous_item_id(self):
        """Test adding new item to end when no previous_item_id is specified"""
        # Create initial history
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        old_history = [item1]

        # Create new item without previous_item_id
        new_item = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Second")]
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), new_item
        )

        # Should add to end
        assert len(new_history) == 2
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"

    def test_add_first_item_to_empty_history(self):
        """Test adding first item to empty history"""
        old_history: list[RealtimeItem] = []

        new_item = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )

        new_history = RealtimeSession._get_new_history(old_history, new_item)

        assert len(new_history) == 1
        assert new_history[0].item_id == "item_1"

    def test_complex_insertion_scenario(self):
        """Test complex scenario with multiple insertions and updates"""
        # Start with items A and C
        itemA = AssistantMessageItem(
            item_id="A", role="assistant", content=[AssistantText(text="A")]
        )
        itemC = AssistantMessageItem(
            item_id="C", role="assistant", content=[AssistantText(text="C")]
        )
        history: list[RealtimeItem] = [itemA, itemC]

        # Insert B after A
        itemB = AssistantMessageItem(
            item_id="B", previous_item_id="A", role="assistant", content=[AssistantText(text="B")]
        )
        history = RealtimeSession._get_new_history(history, itemB)

        # Should be A, B, C
        assert len(history) == 3
        assert [item.item_id for item in history] == ["A", "B", "C"]

        # Insert D after B
        itemD = AssistantMessageItem(
            item_id="D", previous_item_id="B", role="assistant", content=[AssistantText(text="D")]
        )
        history = RealtimeSession._get_new_history(history, itemD)

        # Should be A, B, D, C
        assert len(history) == 4
        assert [item.item_id for item in history] == ["A", "B", "D", "C"]

        # Update B
        updated_itemB = AssistantMessageItem(
            item_id="B", role="assistant", content=[AssistantText(text="Updated B")]
        )
        history = RealtimeSession._get_new_history(history, updated_itemB)

        # Should still be A, B, D, C but B is updated
        assert len(history) == 4
        assert [item.item_id for item in history] == ["A", "B", "D", "C"]
        itemB_result = cast(AssistantMessageItem, history[1])
        assert itemB_result.content[0].text == "Updated B"  # type: ignore


# Test 3: Tool call execution flow (_handle_tool_call method)
class TestToolCallExecution:
    """Test suite for tool call execution flow in RealtimeSession._handle_tool_call"""

    @pytest.mark.asyncio
    async def test_function_tool_execution_success(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Test successful function tool execution"""
        # Set up agent to return our mock tool
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Create function call event
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_123", arguments='{"param": "value"}'
        )

        await session._handle_tool_call(tool_call_event)

        # Verify the flow
        mock_agent.get_all_tools.assert_called_once()
        mock_function_tool.on_invoke_tool.assert_called_once()

        # Check the tool context was created correctly
        call_args = mock_function_tool.on_invoke_tool.call_args
        tool_context = call_args[0][0]
        assert isinstance(tool_context, ToolContext)
        assert call_args[0][1] == '{"param": "value"}'

        # Verify tool output was sent to model
        assert len(mock_model.sent_tool_outputs) == 1
        sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_call == tool_call_event
        assert sent_output == "function_result"
        assert start_response is True

        # Verify events were queued
        assert session._event_queue.qsize() == 2

        # Check tool start event
        tool_start_event = await session._event_queue.get()
        assert isinstance(tool_start_event, RealtimeToolStart)
        assert tool_start_event.tool == mock_function_tool
        assert tool_start_event.agent == mock_agent

        # Check tool end event
        tool_end_event = await session._event_queue.get()
        assert isinstance(tool_end_event, RealtimeToolEnd)
        assert tool_end_event.tool == mock_function_tool
        assert tool_end_event.output == "function_result"
        assert tool_end_event.agent == mock_agent

    @pytest.mark.asyncio
    async def test_function_tool_with_multiple_tools_available(self, mock_model, mock_agent):
        """Test function tool execution when multiple tools are available"""
        # Create multiple mock tools
        tool1 = Mock(spec=FunctionTool)
        tool1.name = "tool_one"
        tool1.on_invoke_tool = AsyncMock(return_value="result_one")

        tool2 = Mock(spec=FunctionTool)
        tool2.name = "tool_two"
        tool2.on_invoke_tool = AsyncMock(return_value="result_two")

        handoff = Mock(spec=Handoff)
        handoff.name = "handoff_tool"

        # Set up agent to return all tools
        mock_agent.get_all_tools.return_value = [tool1, tool2, handoff]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Call tool_two
        tool_call_event = RealtimeModelToolCallEvent(
            name="tool_two", call_id="call_456", arguments='{"test": "data"}'
        )

        await session._handle_tool_call(tool_call_event)

        # Only tool2 should have been called
        tool1.on_invoke_tool.assert_not_called()
        tool2.on_invoke_tool.assert_called_once()

        # Verify correct result was sent
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert sent_output == "result_two"

    @pytest.mark.asyncio
    async def test_handoff_tool_handling(self, mock_model):
        first_agent = RealtimeAgent(
            name="first_agent",
            instructions="first_agent_instructions",
            tools=[],
            handoffs=[],
        )
        second_agent = RealtimeAgent(
            name="second_agent",
            instructions="second_agent_instructions",
            tools=[],
            handoffs=[],
        )

        first_agent.handoffs = [second_agent]

        session = RealtimeSession(mock_model, first_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name=Handoff.default_tool_name(second_agent), call_id="call_789", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Should have sent session update and tool output
        assert len(mock_model.sent_events) >= 2

        # Should have sent handoff event
        assert session._event_queue.qsize() >= 1

        # Verify agent was updated
        assert session._current_agent == second_agent

    @pytest.mark.asyncio
    async def test_unknown_tool_handling(self, mock_model, mock_agent, mock_function_tool):
        """Test that unknown tools raise an error"""
        import pytest

        from agents.exceptions import ModelBehaviorError

        # Set up agent to return different tool than what's called
        mock_function_tool.name = "known_tool"
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Call unknown tool
        tool_call_event = RealtimeModelToolCallEvent(
            name="unknown_tool", call_id="call_unknown", arguments="{}"
        )

        # Should raise an error for unknown tool
        with pytest.raises(ModelBehaviorError, match="Tool unknown_tool not found"):
            await session._handle_tool_call(tool_call_event)

        # Should not have called any tools
        mock_function_tool.on_invoke_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_function_tool_exception_handling(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Test that exceptions in function tools are handled (currently they propagate)"""
        # Set up tool to raise exception
        mock_function_tool.on_invoke_tool.side_effect = ValueError("Tool error")
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_error", arguments="{}"
        )

        # Currently exceptions propagate (no error handling implemented)
        with pytest.raises(ValueError, match="Tool error"):
            await session._handle_tool_call(tool_call_event)

        # Tool start event should have been queued before the error
        assert session._event_queue.qsize() == 1
        tool_start_event = await session._event_queue.get()
        assert isinstance(tool_start_event, RealtimeToolStart)

        # But no tool output should have been sent and no end event queued
        assert len(mock_model.sent_tool_outputs) == 0

    @pytest.mark.asyncio
    async def test_tool_call_with_complex_arguments(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Test tool call with complex JSON arguments"""
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Complex arguments
        complex_args = '{"nested": {"data": [1, 2, 3]}, "bool": true, "null": null}'

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_complex", arguments=complex_args
        )

        await session._handle_tool_call(tool_call_event)

        # Verify arguments were passed correctly
        call_args = mock_function_tool.on_invoke_tool.call_args
        assert call_args[0][1] == complex_args

    @pytest.mark.asyncio
    async def test_tool_call_with_custom_call_id(self, mock_model, mock_agent, mock_function_tool):
        """Test that tool context receives correct call_id"""
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        custom_call_id = "custom_call_id_12345"

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id=custom_call_id, arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Verify tool context was created with correct call_id
        call_args = mock_function_tool.on_invoke_tool.call_args
        tool_context = call_args[0][0]
        # The call_id is used internally in ToolContext.from_agent_context
        # We can't directly access it, but we can verify the context was created
        assert isinstance(tool_context, ToolContext)

    @pytest.mark.asyncio
    async def test_tool_result_conversion_to_string(self, mock_model, mock_agent):
        """Test that tool results are converted to strings for model output"""
        # Create tool that returns non-string result
        tool = Mock(spec=FunctionTool)
        tool.name = "test_function"
        tool.on_invoke_tool = AsyncMock(return_value={"result": "data", "count": 42})

        mock_agent.get_all_tools.return_value = [tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_conversion", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Verify result was converted to string
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert isinstance(sent_output, str)
        assert sent_output == "{'result': 'data', 'count': 42}"

    @pytest.mark.asyncio
    async def test_mixed_tool_types_filtering(self, mock_model, mock_agent):
        """Test that function tools and handoffs are properly separated"""
        # Create mixed tools
        func_tool1 = Mock(spec=FunctionTool)
        func_tool1.name = "func1"
        func_tool1.on_invoke_tool = AsyncMock(return_value="result1")

        handoff1 = Mock(spec=Handoff)
        handoff1.name = "handoff1"

        func_tool2 = Mock(spec=FunctionTool)
        func_tool2.name = "func2"
        func_tool2.on_invoke_tool = AsyncMock(return_value="result2")

        handoff2 = Mock(spec=Handoff)
        handoff2.name = "handoff2"

        # Add some other object that's neither (should be ignored)
        other_tool = Mock()
        other_tool.name = "other"

        all_tools = [func_tool1, handoff1, func_tool2, handoff2, other_tool]
        mock_agent.get_all_tools.return_value = all_tools

        session = RealtimeSession(mock_model, mock_agent, None)

        # Call a function tool
        tool_call_event = RealtimeModelToolCallEvent(
            name="func2", call_id="call_filtering", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Only func2 should have been called
        func_tool1.on_invoke_tool.assert_not_called()
        func_tool2.on_invoke_tool.assert_called_once()

        # Verify result
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert sent_output == "result2"


class TestGuardrailFunctionality:
    """Test suite for output guardrail functionality in RealtimeSession"""

    async def _wait_for_guardrail_tasks(self, session):
        """Wait for all pending guardrail tasks to complete."""
        import asyncio

        if session._guardrail_tasks:
            await asyncio.gather(*session._guardrail_tasks, return_exceptions=True)

    @pytest.fixture
    def triggered_guardrail(self):
        """Creates a guardrail that always triggers"""

        def guardrail_func(context, agent, output):
            return GuardrailFunctionOutput(
                output_info={"reason": "test trigger"}, tripwire_triggered=True
            )

        return OutputGuardrail(guardrail_function=guardrail_func, name="triggered_guardrail")

    @pytest.fixture
    def safe_guardrail(self):
        """Creates a guardrail that never triggers"""

        def guardrail_func(context, agent, output):
            return GuardrailFunctionOutput(
                output_info={"reason": "safe content"}, tripwire_triggered=False
            )

        return OutputGuardrail(guardrail_function=guardrail_func, name="safe_guardrail")

    @pytest.mark.asyncio
    async def test_transcript_delta_triggers_guardrail_at_threshold(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test that guardrails run when transcript delta reaches debounce threshold"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Send transcript delta that exceeds threshold (10 chars)
        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="this is more than ten characters", response_id="resp_1"
        )

        await session.on_event(transcript_event)

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should have triggered guardrail and interrupted
        assert session._interrupted_by_guardrail is True
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
        assert "triggered_guardrail" in mock_model.sent_messages[0]

        # Should have emitted guardrail_tripped event
        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert guardrail_events[0].message == "this is more than ten characters"

    @pytest.mark.asyncio
    async def test_transcript_delta_multiple_thresholds_same_item(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test guardrails run at 1x, 2x, 3x thresholds for same item_id"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # First delta - reaches 1x threshold (5 chars)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="12345", response_id="resp_1")
        )

        # Second delta - reaches 2x threshold (10 chars total)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="67890", response_id="resp_1")
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should only trigger once due to interrupted_by_guardrail flag
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_transcript_delta_different_items_tracked_separately(
        self, mock_model, mock_agent, safe_guardrail
    ):
        """Test that different item_ids are tracked separately for debouncing"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [safe_guardrail],
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Add text to item_1 (8 chars - below threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="12345678", response_id="resp_1"
            )
        )

        # Add text to item_2 (8 chars - below threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_2", delta="abcdefgh", response_id="resp_2"
            )
        )

        # Neither should trigger guardrails yet
        assert mock_model.interrupts_called == 0

        # Add more text to item_1 (total 12 chars - above threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="90ab", response_id="resp_1")
        )

        # item_1 should have triggered guardrail run (but not interrupted since safe)
        assert session._item_guardrail_run_counts["item_1"] == 1
        assert (
            "item_2" not in session._item_guardrail_run_counts
            or session._item_guardrail_run_counts["item_2"] == 0
        )

    @pytest.mark.asyncio
    async def test_turn_ended_clears_guardrail_state(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test that turn_ended event clears guardrail state for next turn"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Trigger guardrail
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="trigger", response_id="resp_1"
            )
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        assert session._interrupted_by_guardrail is True
        assert len(session._item_transcripts) == 1

        # End turn
        await session.on_event(RealtimeModelTurnEndedEvent())

        # State should be cleared
        assert session._interrupted_by_guardrail is False
        assert len(session._item_transcripts) == 0
        assert len(session._item_guardrail_run_counts) == 0

    @pytest.mark.asyncio
    async def test_multiple_guardrails_all_triggered(self, mock_model, mock_agent):
        """Test that all triggered guardrails are included in the event"""

        def create_triggered_guardrail(name):
            def guardrail_func(context, agent, output):
                return GuardrailFunctionOutput(output_info={"name": name}, tripwire_triggered=True)

            return OutputGuardrail(guardrail_function=guardrail_func, name=name)

        guardrail1 = create_triggered_guardrail("guardrail_1")
        guardrail2 = create_triggered_guardrail("guardrail_2")

        run_config: RealtimeRunConfig = {
            "output_guardrails": [guardrail1, guardrail2],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="trigger", response_id="resp_1"
            )
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should have interrupted and sent message with both guardrail names
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
        message = mock_model.sent_messages[0]
        assert "guardrail_1" in message and "guardrail_2" in message

        # Should have emitted event with both guardrail results
        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert len(guardrail_events[0].guardrail_results) == 2
