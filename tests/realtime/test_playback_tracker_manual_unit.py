from agents.realtime.model import RealtimePlaybackTracker


def test_playback_tracker_on_play_bytes_and_state():
    tr = RealtimePlaybackTracker()
    tr.set_audio_format("pcm16")  # PCM path

    # 48k bytes -> (48000 / (24000 * 2)) * 1000 = 1_000ms
    tr.on_play_bytes("item1", 0, b"x" * 48000)
    st = tr.get_state()
    assert st["current_item_id"] == "item1"
    assert st["elapsed_ms"] and abs(st["elapsed_ms"] - 1_000.0) < 1e-6

    # Subsequent play on same item accumulates
    tr.on_play_ms("item1", 0, 500.0)
    st2 = tr.get_state()
    assert st2["elapsed_ms"] and abs(st2["elapsed_ms"] - 1_500.0) < 1e-6

    # Interruption clears state
    tr.on_interrupted()
    st3 = tr.get_state()
    assert st3["current_item_id"] is None
    assert st3["elapsed_ms"] is None


def test_playback_tracker_on_play_bytes_accepts_audio_bytes_keyword():
    """on_play_bytes should expose `audio_bytes` (not the shadowed builtin
    name `bytes`) so callers can reliably pass it as a keyword argument."""
    tr = RealtimePlaybackTracker()
    tr.set_audio_format("pcm16")

    tr.on_play_bytes(item_id="item1", item_content_index=0, audio_bytes=b"x" * 24000)
    state = tr.get_state()
    assert state["current_item_id"] == "item1"
    assert state["current_item_content_index"] == 0
    assert state["elapsed_ms"] and abs(state["elapsed_ms"] - 500.0) < 1e-6
