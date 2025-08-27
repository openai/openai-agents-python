from unittest.mock import AsyncMock

import pytest

from agents.realtime._default_tracker import ModelAudioTracker
from agents.realtime.model import RealtimePlaybackTracker
from agents.realtime.model_inputs import RealtimeModelSendInterrupt
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel


class TestPlaybackTracker:
    """Test playback tracker functionality for interrupt timing."""

    @pytest.fixture
    def model(self):
        """Create a fresh model instance for each test."""
        return OpenAIRealtimeWebSocketModel()

    @pytest.mark.asyncio
    async def test_interrupt_timing_with_custom_playback_tracker(self, model):
        """Test interrupt uses custom playback tracker elapsed time instead of default timing."""

        # Create custom tracker and set elapsed time
        custom_tracker = RealtimePlaybackTracker()
        custom_tracker.set_audio_format("pcm16")
        custom_tracker.on_play_ms("item_1", 1, 500.0)  # content_index 1, 500ms played

        # Set up model with custom tracker directly
        model._playback_tracker = custom_tracker

        # Mock send_raw_message to capture interrupt
        model._send_raw_message = AsyncMock()

        # Send interrupt

        await model._send_interrupt(RealtimeModelSendInterrupt())

        # Should use custom tracker's 500ms elapsed time
        model._send_raw_message.assert_called_once()
        call_args = model._send_raw_message.call_args[0][0]
        assert call_args.audio_end_ms == 500

    @pytest.mark.asyncio
    async def test_interrupt_skipped_when_no_audio_playing(self, model):
        """Test interrupt returns early when no audio is currently playing."""
        model._send_raw_message = AsyncMock()

        # No audio playing (default state)

        await model._send_interrupt(RealtimeModelSendInterrupt())

        # Should not send any interrupt message
        model._send_raw_message.assert_not_called()

    def test_audio_state_accumulation_across_deltas(self):
        """Test ModelAudioTracker accumulates audio length across multiple deltas."""

        tracker = ModelAudioTracker()
        tracker.set_audio_format("pcm16")

        # Send multiple deltas for same item
        tracker.on_audio_delta("item_1", 0, b"test")  # 4 bytes
        tracker.on_audio_delta("item_1", 0, b"more")  # 4 bytes

        state = tracker.get_state("item_1", 0)
        assert state is not None
        # Should accumulate: 8 bytes / 24 / 2 * 1000 = 166.67ms
        expected_length = (8 / 24 / 2) * 1000
        assert abs(state.audio_length_ms - expected_length) < 0.01

    def test_state_cleanup_on_interruption(self):
        """Test both trackers properly reset state on interruption."""

        # Test ModelAudioTracker cleanup
        model_tracker = ModelAudioTracker()
        model_tracker.set_audio_format("pcm16")
        model_tracker.on_audio_delta("item_1", 0, b"test")
        assert model_tracker.get_last_audio_item() == ("item_1", 0)

        model_tracker.on_interrupted()
        assert model_tracker.get_last_audio_item() is None

        # Test RealtimePlaybackTracker cleanup
        playback_tracker = RealtimePlaybackTracker()
        playback_tracker.on_play_ms("item_1", 0, 100.0)

        state = playback_tracker.get_state()
        assert state["current_item_id"] == "item_1"
        assert state["elapsed_ms"] == 100.0

        playback_tracker.on_interrupted()
        state = playback_tracker.get_state()
        assert state["current_item_id"] is None
        assert state["elapsed_ms"] is None

    def test_audio_length_calculation_with_different_formats(self):
        """Test calculate_audio_length_ms handles g711 and PCM formats correctly."""
        from agents.realtime._util import calculate_audio_length_ms

        # Test g711 format (8kHz)
        g711_bytes = b"12345678"  # 8 bytes
        g711_length = calculate_audio_length_ms("g711_ulaw", g711_bytes)
        assert g711_length == 1  # (8 / 8000) * 1000

        # Test PCM format (24kHz, default)
        pcm_bytes = b"test"  # 4 bytes
        pcm_length = calculate_audio_length_ms("pcm16", pcm_bytes)
        assert pcm_length == (4 / 24 / 2) * 1000  # ~83.33ms

        # Test None format (defaults to PCM)
        none_length = calculate_audio_length_ms(None, pcm_bytes)
        assert none_length == pcm_length
