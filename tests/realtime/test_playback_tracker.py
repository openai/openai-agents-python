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
        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert truncate_events
        assert truncate_events[0].audio_end_ms == 500

    @pytest.mark.asyncio
    async def test_interrupt_skipped_when_no_audio_playing(self, model):
        """Test interrupt returns early when no audio is currently playing."""
        model._send_raw_message = AsyncMock()

        # No audio playing (default state)

        await model._send_interrupt(RealtimeModelSendInterrupt())

        # Should not send any interrupt message
        model._send_raw_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_interrupt_skips_when_elapsed_exceeds_audio_length(self, model):
        """Test interrupt skips truncation when playback appears complete."""
        model._send_raw_message = AsyncMock()
        model._audio_state_tracker.set_audio_format("pcm16")

        # 48_000 bytes of PCM16 at 24kHz equals ~1000ms of audio.
        model._audio_state_tracker.on_audio_delta("item_1", 0, b"a" * 48_000)
        model._playback_tracker = RealtimePlaybackTracker()
        model._playback_tracker.on_play_ms("item_1", 0, 2000.0)

        await model._send_interrupt(RealtimeModelSendInterrupt())

        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert truncate_events == []

    @pytest.mark.asyncio
    async def test_interrupt_sends_truncate_when_ongoing_response(self, model):
        """Test interrupt still truncates while response is ongoing."""
        model._ongoing_response = True
        model._send_raw_message = AsyncMock()
        model._audio_state_tracker.set_audio_format("pcm16")

        # 48_000 bytes of PCM16 at 24kHz equals ~1000ms of audio.
        model._audio_state_tracker.on_audio_delta("item_1", 0, b"a" * 48_000)
        model._playback_tracker = RealtimePlaybackTracker()
        model._playback_tracker.on_play_ms("item_1", 0, 2000.0)

        await model._send_interrupt(RealtimeModelSendInterrupt())

        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert truncate_events
        assert truncate_events[0].audio_end_ms == 2000

    def test_audio_delta_before_set_audio_format_does_not_raise(self):
        """ModelAudioTracker must tolerate audio deltas before a format is negotiated.

        For transcription-only sessions or session payloads that omit an audio
        format, ``set_audio_format`` is never called. Previously, the first
        ``on_audio_delta`` call raised ``AttributeError`` because ``self._format``
        was unset. The length calculator already accepts ``None`` as the
        unknown-format fallback, so the tracker should pass that through.
        """

        tracker = ModelAudioTracker()
        # Intentionally do NOT call set_audio_format here.
        tracker.on_audio_delta("item_1", 0, b"test")

        state = tracker.get_state("item_1", 0)
        assert state is not None
        # With no format, calculate_audio_length_ms falls back to PCM math.
        expected_length = (4 / (24_000 * 2)) * 1000
        assert state.audio_length_ms == pytest.approx(expected_length, rel=0, abs=1e-6)
        assert tracker.get_last_audio_item() == ("item_1", 0)

    def test_audio_state_accumulation_across_deltas(self):
        """Test ModelAudioTracker accumulates audio length across multiple deltas."""

        tracker = ModelAudioTracker()
        tracker.set_audio_format("pcm16")

        # Send multiple deltas for same item
        tracker.on_audio_delta("item_1", 0, b"test")  # 4 bytes
        tracker.on_audio_delta("item_1", 0, b"more")  # 4 bytes

        state = tracker.get_state("item_1", 0)
        assert state is not None
        # Should accumulate: 8 bytes -> 4 samples -> (4 / 24000) * 1000 ≈ 0.167ms
        expected_length = (8 / (24_000 * 2)) * 1000
        assert state.audio_length_ms == pytest.approx(expected_length, rel=0, abs=1e-6)

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
        expected_pcm = (len(pcm_bytes) / (24_000 * 2)) * 1000
        assert pcm_length == pytest.approx(expected_pcm, rel=0, abs=1e-6)

        # Test None format (defaults to PCM)
        none_length = calculate_audio_length_ms(None, pcm_bytes)
        assert none_length == pytest.approx(expected_pcm, rel=0, abs=1e-6)

    def test_audio_length_calculation_handles_typed_and_mapping_g711_formats(self):
        """g711 audio passed as a typed pydantic model, Mapping, or ``audio/pcm*`` string
        must be measured at the g711 sample rate.

        ``RealtimePlaybackTracker.set_audio_format`` and ``ModelAudioTracker.set_audio_format``
        accept ``RealtimeAudioFormat``, which is ``str | Mapping | AudioPCM/PCMU/PCMA``.
        Previously the length calculator only special-cased strings starting with
        ``g711``, so typed/Mapping g711 formats and the ``audio/pcmu``/``audio/pcma``
        strings silently fell back to PCM-24kHz math, yielding a ~6x wrong duration
        and miscalculating truncation offsets on interrupt for SIP/Twilio sessions.
        """
        from openai.types.realtime.realtime_audio_formats import (
            AudioPCM,
            AudioPCMA,
            AudioPCMU,
        )

        from agents.realtime._util import calculate_audio_length_ms

        audio_bytes = b"x" * 80  # at g711 8kHz: 10ms
        expected_g711 = (len(audio_bytes) / 8_000) * 1000
        expected_pcm = (len(audio_bytes) / (24_000 * 2)) * 1000

        # Typed pydantic models for g711 should resolve to g711 sample rate.
        assert calculate_audio_length_ms(
            AudioPCMU(type="audio/pcmu"), audio_bytes
        ) == pytest.approx(expected_g711, rel=0, abs=1e-6)
        assert calculate_audio_length_ms(
            AudioPCMA(type="audio/pcma"), audio_bytes
        ) == pytest.approx(expected_g711, rel=0, abs=1e-6)
        # Typed PCM and Mapping/string equivalents stay on the PCM path.
        assert calculate_audio_length_ms(
            AudioPCM(type="audio/pcm", rate=24000), audio_bytes
        ) == pytest.approx(expected_pcm, rel=0, abs=1e-6)

        # Mapping forms (as accepted by RealtimeAudioFormat).
        assert calculate_audio_length_ms({"type": "audio/pcmu"}, audio_bytes) == pytest.approx(
            expected_g711, rel=0, abs=1e-6
        )
        assert calculate_audio_length_ms({"type": "audio/pcma"}, audio_bytes) == pytest.approx(
            expected_g711, rel=0, abs=1e-6
        )

        # API-style ``audio/pcm*`` strings should also be honored.
        assert calculate_audio_length_ms("audio/pcmu", audio_bytes) == pytest.approx(
            expected_g711, rel=0, abs=1e-6
        )
        assert calculate_audio_length_ms("audio/pcma", audio_bytes) == pytest.approx(
            expected_g711, rel=0, abs=1e-6
        )
