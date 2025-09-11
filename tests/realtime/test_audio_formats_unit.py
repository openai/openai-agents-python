from openai.types.realtime.realtime_audio_formats import AudioPCM

from agents.realtime.audio_formats import to_realtime_audio_format


def test_to_realtime_audio_format_from_strings():
    assert to_realtime_audio_format("pcm").type == "audio/pcm"  # type: ignore[union-attr]
    assert to_realtime_audio_format("pcm16").type == "audio/pcm"  # type: ignore[union-attr]
    assert to_realtime_audio_format("audio/pcm").type == "audio/pcm"  # type: ignore[union-attr]
    assert to_realtime_audio_format("pcmu").type == "audio/pcmu"  # type: ignore[union-attr]
    assert to_realtime_audio_format("audio/pcmu").type == "audio/pcmu"  # type: ignore[union-attr]
    assert to_realtime_audio_format("g711_ulaw").type == "audio/pcmu"  # type: ignore[union-attr]
    assert to_realtime_audio_format("pcma").type == "audio/pcma"  # type: ignore[union-attr]
    assert to_realtime_audio_format("audio/pcma").type == "audio/pcma"  # type: ignore[union-attr]
    assert to_realtime_audio_format("g711_alaw").type == "audio/pcma"  # type: ignore[union-attr]


def test_to_realtime_audio_format_passthrough_and_unknown_logs():
    fmt = AudioPCM(type="audio/pcm", rate=24000)
    # Passing a RealtimeAudioFormats should return the same instance
    assert to_realtime_audio_format(fmt) is fmt

    # Unknown string returns None (and logs at debug level internally)
    assert to_realtime_audio_format("something_else") is None


def test_to_realtime_audio_format_none():
    assert to_realtime_audio_format(None) is None
