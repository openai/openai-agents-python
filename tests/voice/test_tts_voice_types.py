from typing import get_args

from agents.voice.model import TTSVoice


def test_tts_voice_type_includes_current_openai_builtin_voices() -> None:
    assert {"ballad", "verse", "marin", "cedar"} <= set(get_args(TTSVoice))
