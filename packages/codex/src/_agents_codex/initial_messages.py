"""Capture settled messages before opening a fresh native conversation."""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from ._media import _MAX_AUDIO_INPUT_BYTES, _audio_json, _check_audio_json, _contains_audio
from ._public import preserve_module_identity as _preserve_module_identity
from .input import AssistantMessage, InitialMessage, UserMessage, _native_input, _response_content


def _initial_messages_json(messages: Sequence[InitialMessage] | None) -> str | None:
    return None if messages is None else _audio_json(messages)


def _capture_initial_messages(
    messages: Sequence[InitialMessage] | None,
    *,
    state: Any = None,
    resume: str | None = None,
    _audio_budget: list[int] | None = None,
) -> list[InitialMessage] | None:
    if messages is None:
        return None
    if state is not None or resume is not None:
        raise ValueError("initial_messages requires a fresh conversation without state or resume")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise TypeError("initial_messages must be a sequence of settled messages")
    captured: list[InitialMessage] = []
    audio_budget = [_MAX_AUDIO_INPUT_BYTES] if _audio_budget is None else _audio_budget
    contains_audio = _contains_audio(messages)
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("initial_messages entries must be user or assistant messages")
        content = message.get("content")
        if message.get("role") == "user":
            parts = _response_content(
                _native_input(cast(UserMessage, message), _audio_budget=audio_budget)
            )
            for part in parts:
                for value in part.values():
                    if isinstance(value, str):
                        _check_audio_json(value, contains_audio=contains_audio)
                        value.encode("utf-8")
            captured.append(cast(UserMessage, {"role": "user", "content": parts}))
        elif (
            message.get("role") == "assistant"
            and message.get("type", "message") == "message"
            and not set(message) - {"role", "content", "type"}
            and isinstance(content, str)
        ):
            _check_audio_json(content, contains_audio=contains_audio)
            content.encode("utf-8")
            assistant: AssistantMessage = {"role": "assistant", "content": content}
            captured.append(assistant)
        else:
            raise ValueError(
                "initial_messages accepts only user messages and settled assistant text"
            )
    _check_audio_json(captured)
    return captured


_preserve_module_identity(globals(), "agents.input")
