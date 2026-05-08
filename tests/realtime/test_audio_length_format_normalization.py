"""Tests for audio length calculation format normalization.

`calculate_audio_length_ms` must correctly identify g711 audio regardless of
how the format was supplied (string, mapping, or pydantic model). Previously
non-string formats fell through to the PCM16 path, computing a wrong duration
for g711 audio.
"""

from openai.types.realtime.realtime_audio_formats import AudioPCM, AudioPCMA, AudioPCMU

from agents.realtime._util import calculate_audio_length_ms


def test_calculate_audio_length_ms_pydantic_g711_models() -> None:
    # 8 bytes of g711 audio at 8 kHz -> 1 ms
    assert calculate_audio_length_ms(AudioPCMU(type="audio/pcmu"), b"a" * 8) == 1.0
    assert calculate_audio_length_ms(AudioPCMA(type="audio/pcma"), b"a" * 8) == 1.0


def test_calculate_audio_length_ms_pydantic_pcm_model() -> None:
    # 48 bytes of pcm16 at 24 kHz / 2 bytes per sample -> 1 ms
    assert calculate_audio_length_ms(AudioPCM(type="audio/pcm", rate=24000), b"a" * 48) == 1.0


def test_calculate_audio_length_ms_mapping_formats() -> None:
    assert calculate_audio_length_ms({"type": "audio/pcmu"}, b"a" * 8) == 1.0
    assert calculate_audio_length_ms({"type": "audio/pcma"}, b"a" * 8) == 1.0
    assert calculate_audio_length_ms({"type": "audio/pcm"}, b"a" * 48) == 1.0


def test_calculate_audio_length_ms_audio_pcmu_string_alias() -> None:
    # The raw API string "audio/pcmu" should also be recognized as g711.
    assert calculate_audio_length_ms("audio/pcmu", b"a" * 8) == 1.0
    assert calculate_audio_length_ms("audio/pcma", b"a" * 8) == 1.0


def test_calculate_audio_length_ms_uppercase_string() -> None:
    # Case-insensitive g711 detection.
    assert calculate_audio_length_ms("G711_ULAW", b"a" * 8) == 1.0


def test_calculate_audio_length_ms_unknown_mapping_falls_back_to_pcm() -> None:
    # Unknown formats keep the historical PCM16 fallback for backward compat.
    assert calculate_audio_length_ms({"type": "audio/unknown"}, b"a" * 48) == 1.0


def test_calculate_audio_length_ms_empty_bytes_zero() -> None:
    assert calculate_audio_length_ms(AudioPCMU(type="audio/pcmu"), b"") == 0.0
    assert calculate_audio_length_ms(None, b"") == 0.0
