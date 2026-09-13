"""Realtime item history updates and transcript preservation."""

from typing import Any, cast
from unittest.mock import patch

import pytest

import agents._debug as _debug
from agents.realtime.agent import RealtimeAgent
from agents.realtime.events import (
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
)
from agents.realtime.items import (
    AssistantAudio,
    AssistantMessageItem,
    AssistantText,
    InputAudio,
    InputText,
    RealtimeItem,
    RealtimeToolCallItem,
    UserMessageItem,
)
from agents.realtime.model_events import (
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelTranscriptDeltaEvent,
)
from agents.realtime.session import (
    RealtimeSession,
)

from . import session_test_support
from .session_test_support import _DummyModel

# Bind shared fixtures explicitly so unrelated Realtime modules do not inherit them.
mock_agent = session_test_support.mock_agent
mock_model = session_test_support.mock_model


@pytest.mark.asyncio
async def test_transcription_completed_adds_new_user_item():
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    event = RealtimeModelInputAudioTranscriptionCompletedEvent(item_id="item1", transcript="hello")
    await session.on_event(event)

    # Should have appended a new user item
    assert len(session._history) == 1
    assert session._history[0].type == "message"
    assert session._history[0].role == "user"


class _FakeAudio:
    # Looks like an audio part but is not an InputAudio/AssistantAudio instance
    type = "audio"
    transcript = None


@pytest.mark.asyncio
async def test_item_updated_merge_exception_path_logs_error(monkeypatch):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    # existing assistant message with transcript to preserve
    existing = AssistantMessageItem(
        item_id="a1", role="assistant", content=[AssistantAudio(audio=None, transcript="t")]
    )
    session._history = [existing]

    # incoming message with a deliberately bogus content entry to trigger assertion path
    incoming = AssistantMessageItem(
        item_id="a1", role="assistant", content=[AssistantAudio(audio=None, transcript=None)]
    )
    incoming.content[0] = cast(Any, _FakeAudio())

    with patch("agents.realtime.session.logger") as mock_logger:
        await session.on_event(RealtimeModelItemUpdatedEvent(item=incoming))
        mock_logger.error.assert_called_once_with("%s", "Error merging transcripts", stacklevel=3)


class TestEventHandling:
    """Test suite for event handling and transformation in RealtimeSession.on_event"""

    @pytest.mark.asyncio
    async def test_transcription_completed_event_updates_history(self, mock_model, mock_agent):
        """Test that transcription completed events update history and emit events"""
        session = RealtimeSession(
            mock_model, mock_agent, None, run_config={"async_tool_calls": False}
        )

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
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

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
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

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
    async def test_item_updated_event_completes_tool_call(self, mock_model, mock_agent):
        """The transport reuses one item for a tool call and its output, so the second
        item_updated must land in history."""
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        in_progress = RealtimeToolCallItem(
            item_id="fc_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="in_progress",
            arguments='{"city": "Oakland"}',
            name="get_weather",
            output=None,
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=in_progress))

        completed = in_progress.model_copy(update={"status": "completed", "output": "sunny"})
        await session.on_event(RealtimeModelItemUpdatedEvent(item=completed))

        assert len(session._history) == 1
        stored = cast(RealtimeToolCallItem, session._history[0])
        assert stored.status == "completed"
        assert stored.output == "sunny"

        # raw + history added, then raw + history updated.
        assert session._event_queue.qsize() == 4
        await session._event_queue.get()  # raw event
        assert isinstance(await session._event_queue.get(), RealtimeHistoryAdded)
        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)
        assert cast(RealtimeToolCallItem, history_event.history[0]).output == "sunny"

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

    def test_tool_call_item_update_replaces_existing_entry(self):
        """A completed tool call replaces the in-progress entry it shares an item_id with."""
        in_progress = RealtimeToolCallItem(
            item_id="item_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="in_progress",
            arguments='{"city": "Oakland"}',
            name="get_weather",
            output=None,
        )
        completed = RealtimeToolCallItem(
            item_id="item_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="completed",
            arguments='{"city": "Oakland"}',
            name="get_weather",
            output="sunny",
        )

        history = RealtimeSession._get_new_history([], in_progress)
        history = RealtimeSession._get_new_history(history, completed)

        assert len(history) == 1
        updated = cast(RealtimeToolCallItem, history[0])
        assert updated.status == "completed"
        assert updated.output == "sunny"

    def test_tool_call_item_update_preserves_other_items(self):
        """Replacing a tool call entry leaves the surrounding history untouched."""
        before = UserMessageItem(
            item_id="item_0", role="user", content=[InputText(text="what's the weather?")]
        )
        after = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="It is sunny.")]
        )
        in_progress = RealtimeToolCallItem(
            item_id="item_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="in_progress",
            arguments="{}",
            name="get_weather",
            output=None,
        )
        old_history = cast(list[RealtimeItem], [before, in_progress, after])

        completed = in_progress.model_copy(update={"status": "completed", "output": "sunny"})
        new_history = RealtimeSession._get_new_history(old_history, completed)

        assert [item.item_id for item in new_history] == ["item_0", "item_1", "item_2"]
        assert new_history[0] == before
        assert new_history[2] == after
        assert cast(RealtimeToolCallItem, new_history[1]).output == "sunny"

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


class TestTranscriptPreservation:
    """Tests ensuring assistant transcripts are preserved across updates."""

    @pytest.mark.asyncio
    async def test_assistant_transcript_preserved_on_item_update(self, mock_model, mock_agent):
        session = RealtimeSession(mock_model, mock_agent, None)

        # Initial assistant message with audio transcript present (e.g., from first turn)
        initial_item = AssistantMessageItem(
            item_id="assist_1",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript="Hello there")],
        )
        session._history = [initial_item]

        # Later, the platform retrieves/updates the same item but without transcript populated
        updated_without_transcript = AssistantMessageItem(
            item_id="assist_1",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )

        await session.on_event(RealtimeModelItemUpdatedEvent(item=updated_without_transcript))

        # Transcript should be preserved from existing history
        assert len(session._history) == 1
        preserved_item = cast(AssistantMessageItem, session._history[0])
        assert isinstance(preserved_item.content[0], AssistantAudio)
        assert preserved_item.content[0].transcript == "Hello there"

    @pytest.mark.asyncio
    async def test_assistant_transcript_can_fallback_to_deltas(self, mock_model, mock_agent):
        session = RealtimeSession(mock_model, mock_agent, None)

        # Simulate transcript deltas accumulated for an assistant item during generation
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="assist_2", delta="partial transcript", response_id="resp_2"
            )
        )

        # Add initial assistant message without transcript
        initial_item = AssistantMessageItem(
            item_id="assist_2",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=initial_item))

        # Later update still lacks transcript; merge should fallback to accumulated deltas
        update_again = AssistantMessageItem(
            item_id="assist_2",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=update_again))

        preserved_item = cast(AssistantMessageItem, session._history[0])
        assert isinstance(preserved_item.content[0], AssistantAudio)
        assert preserved_item.content[0].transcript == "partial transcript"

    @pytest.mark.asyncio
    async def test_existing_transcript_not_overwritten_by_stale_deltas(
        self, mock_model, mock_agent
    ):
        """Existing transcripts must take precedence over leftover delta accumulators.

        ``_item_transcripts`` is keyed by item_id and persists across updates within a
        turn. When the model retrieves an item without a transcript, the merge should
        fall back to deltas only when no existing transcript is present – otherwise
        the complete transcript already in history would be clobbered by partial
        (or stale) delta state.
        """
        session = RealtimeSession(mock_model, mock_agent, None)

        # History already has the completed transcript for the item.
        initial_item = AssistantMessageItem(
            item_id="assist_3",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript="Final complete transcript")],
        )
        session._history = [initial_item]

        # Simulate stale/leftover delta state for the same item id.
        session._item_transcripts["assist_3"] = "stale partial"

        # Update arrives without transcript populated; merge must keep the existing
        # complete transcript rather than reverting to the stale delta accumulator.
        update_without_transcript = AssistantMessageItem(
            item_id="assist_3",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=update_without_transcript))

        preserved_item = cast(AssistantMessageItem, session._history[0])
        assert isinstance(preserved_item.content[0], AssistantAudio)
        assert preserved_item.content[0].transcript == "Final complete transcript"
