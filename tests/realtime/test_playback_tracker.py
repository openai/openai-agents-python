from unittest.mock import AsyncMock, patch

import pytest
from openai.types.realtime.realtime_audio_formats import AudioPCM, AudioPCMA, AudioPCMU

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
        # The truncation point stays within the audio the client actually received.
        assert truncate_events[0].audio_end_ms == 1000

    @pytest.mark.asyncio
    async def test_interrupt_clamps_truncate_to_received_audio_while_response_ongoing(self, model):
        """Default timing must not truncate past the audio the client received.

        Without a custom playback tracker the elapsed time is wall clock since the first
        audio delta, so it outgrows the received audio whenever the model pauses between
        deltas. The Realtime API rejects a truncate whose ``audio_end_ms`` exceeds the
        item's audio duration, so the value has to be clamped.
        """
        model._ongoing_response = True
        model._send_raw_message = AsyncMock()
        model._audio_state_tracker.set_audio_format("pcm16")

        # 48_000 bytes of PCM16 at 24kHz equals ~1000ms of audio.
        with patch("agents.realtime._default_tracker.time.monotonic", return_value=100.0):
            model._audio_state_tracker.on_audio_delta("item_1", 0, b"a" * 48_000)

        with patch("agents.realtime.openai_realtime.time.monotonic", return_value=105.0):
            await model._send_interrupt(RealtimeModelSendInterrupt())

        truncate_events = [
            call.args[0]
            for call in model._send_raw_message.await_args_list
            if getattr(call.args[0], "type", None) == "conversation.item.truncate"
        ]
        assert truncate_events
        assert truncate_events[0].audio_end_ms == 1000

    @pytest.mark.asyncio
    async def test_interrupt_matches_speech_started_truncation_point(self, model, monkeypatch):
        """Explicit interrupts and VAD barge-in must truncate at the same point."""

        async def truncate_ms_for(interrupt: bool) -> int:
            fresh = OpenAIRealtimeWebSocketModel()
            fresh._ongoing_response = True
            send_raw = AsyncMock()
            monkeypatch.setattr(fresh, "_send_raw_message", send_raw)
            fresh._audio_state_tracker.set_audio_format("pcm16")

            with patch("agents.realtime._default_tracker.time.monotonic", return_value=100.0):
                fresh._audio_state_tracker.on_audio_delta("item_1", 0, b"a" * 48_000)

            with patch("agents.realtime.openai_realtime.time.monotonic", return_value=105.0):
                if interrupt:
                    await fresh._send_interrupt(RealtimeModelSendInterrupt())
                else:
                    await fresh._handle_ws_event(
                        {
                            "type": "input_audio_buffer.speech_started",
                            "event_id": "e1",
                            "item_id": "item_1",
                            "audio_start_ms": 0,
                            "audio_end_ms": 0,
                        }
                    )

            truncate_events = [
                call.args[0]
                for call in send_raw.await_args_list
                if getattr(call.args[0], "type", None) == "conversation.item.truncate"
            ]
            assert truncate_events
            audio_end_ms: int = truncate_events[0].audio_end_ms
            return audio_end_ms

        assert await truncate_ms_for(interrupt=True) == await truncate_ms_for(interrupt=False)

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

    def test_default_playback_timing_uses_monotonic_clock(self, model):
        model._audio_state_tracker.set_audio_format("pcm16")

        with patch("agents.realtime._default_tracker.time.monotonic", return_value=42.0):
            model._audio_state_tracker.on_audio_delta("item_1", 0, b"test")

        with patch("agents.realtime.openai_realtime.time.monotonic", return_value=42.25):
            state = model._get_playback_state()

        assert state["current_item_id"] == "item_1"
        assert state["current_item_content_index"] == 0
        assert state["elapsed_ms"] == pytest.approx(250.0)

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

    @pytest.mark.parametrize(
        "audio_format",
        [
            AudioPCMU(type="audio/pcmu"),
            AudioPCMA(type="audio/pcma"),
            {"type": "audio/pcmu"},
            {"type": "audio/pcma"},
            "audio/pcmu",
            "audio/pcma",
        ],
        ids=["typed-ulaw", "typed-alaw", "mapping-ulaw", "mapping-alaw", "str-ulaw", "str-alaw"],
    )
    def test_g711_length_is_correct_for_every_format_spelling(self, audio_format):
        """G.711 is one byte per sample at 8 kHz regardless of how the format is spelled.

        Only the legacy `"g711_*"` strings were recognized, so the GA wire-format mapping and
        the typed objects fell through to PCM16 math: 2 bytes per sample at 24 kHz, a 6x
        shorter duration. That skews playback and interruption tracking for telephony
        sessions, which are exactly the sessions that use G.711.
        """
        from agents.realtime._util import calculate_audio_length_ms

        # 8000 bytes of G.711 is exactly one second of audio.
        assert calculate_audio_length_ms(audio_format, b"\x00" * 8000) == 1000.0

    @pytest.mark.parametrize(
        "audio_format",
        [AudioPCM(type="audio/pcm", rate=24000), {"type": "audio/pcm"}, "audio/pcm"],
        ids=["typed", "mapping", "str"],
    )
    def test_pcm_spellings_keep_pcm16_math(self, audio_format):
        from agents.realtime._util import calculate_audio_length_ms

        # 48000 bytes of PCM16 at 24 kHz is exactly one second of audio.
        assert calculate_audio_length_ms(audio_format, b"\x00" * 48000) == 1000.0

    def test_playback_tracker_measures_g711_with_a_typed_format(self):
        """End to end: a tracker configured with the GA typed format reports 8 kHz progress."""
        playback_tracker = RealtimePlaybackTracker()
        playback_tracker.set_audio_format(AudioPCMU(type="audio/pcmu"))

        playback_tracker.on_play_bytes("item_1", 0, b"\x00" * 4000)

        assert playback_tracker.get_state()["elapsed_ms"] == 500.0
