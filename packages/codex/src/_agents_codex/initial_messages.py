"""Capture settled messages before opening a fresh native conversation."""

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ._media import _MAX_AUDIO_INPUT_BYTES
from ._public import preserve_module_identity as _preserve_module_identity
from .input import AssistantMessage, InitialMessage, UserMessage, _native_input, _response_content


def _initial_messages_json(messages: Sequence[InitialMessage] | None) -> str | None:
    return None if messages is None else json.dumps(messages)


def _capture_initial_messages(
    messages: Sequence[InitialMessage] | None, *, state: Any = None, resume: str | None = None
) -> list[InitialMessage] | None:
    if messages is None:
        return None
    if state is not None or resume is not None:
        raise ValueError("initial_messages requires a fresh conversation without state or resume")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise TypeError("initial_messages must be a sequence of settled messages")
    captured: list[InitialMessage] = []
    audio_budget = [_MAX_AUDIO_INPUT_BYTES]
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
                        value.encode("utf-8")
            captured.append(cast(UserMessage, {"role": "user", "content": parts}))
        elif (
            message.get("role") == "assistant"
            and message.get("type", "message") == "message"
            and not set(message) - {"role", "content", "type"}
            and isinstance(content, str)
        ):
            content.encode("utf-8")
            assistant: AssistantMessage = {"role": "assistant", "content": content}
            captured.append(assistant)
        else:
            raise ValueError(
                "initial_messages accepts only user messages and settled assistant text"
            )
    return captured


_preserve_module_identity(globals(), "agents.input")
