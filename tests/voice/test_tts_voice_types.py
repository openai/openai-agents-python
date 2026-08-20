from typing import Literal, get_args, get_origin

from agents.voice.model import TTSCustomVoice, TTSModelSettings, TTSVoice


def _builtin_voice_values() -> set[str]:
    literal_type = next(arg for arg in get_args(TTSVoice) if get_origin(arg) is Literal)
    return set(get_args(literal_type))


def test_tts_voice_type_includes_current_openai_builtin_voices() -> None:
    assert {"ballad", "verse", "marin", "cedar"} <= _builtin_voice_values()


def test_tts_voice_type_accepts_custom_voice_ids() -> None:
    custom_voice: TTSCustomVoice = {"id": "voice_1234"}
    settings = TTSModelSettings(voice=custom_voice)

    assert TTSCustomVoice in get_args(TTSVoice)
    assert settings.voice == {"id": "voice_1234"}
