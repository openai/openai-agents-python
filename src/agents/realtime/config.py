from __future__ import annotations

import inspect
from typing import (
    Any,
    Callable,
    Literal,
    Union,
)

from typing_extensions import NotRequired, TypeAlias, TypedDict

from ..model_settings import ToolChoice
from ..tool import FunctionTool
from ..util._types import MaybeAwaitable


class RealtimeClientMessage(TypedDict):
    type: str  # explicitly required
    other_data: NotRequired[dict[str, Any]]


class UserInputText(TypedDict):
    type: Literal["input_text"]
    text: str


class RealtimeUserInputMessage(TypedDict):
    type: Literal["message"]
    role: Literal["user"]
    content: list[UserInputText]


RealtimeUserInput: TypeAlias = Union[str, RealtimeUserInputMessage]


RealtimeAudioFormat: TypeAlias = Union[Literal["pcm16", "g711_ulaw", "g711_alaw"], str]


class RealtimeInputAudioTranscriptionConfig(TypedDict):
    language: NotRequired[str]
    model: NotRequired[Literal["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"] | str]
    prompt: NotRequired[str]


class RealtimeTurnDetectionConfig(TypedDict):
    """Turn detection config. Allows extra vendor keys if needed."""

    type: NotRequired[Literal["semantic_vad", "server_vad"]]
    create_response: NotRequired[bool]
    eagerness: NotRequired[Literal["auto", "low", "medium", "high"]]
    interrupt_response: NotRequired[bool]
    prefix_padding_ms: NotRequired[int]
    silence_duration_ms: NotRequired[int]
    threshold: NotRequired[float]


class RealtimeSessionConfig(TypedDict):
    api_key: NotRequired[APIKeyOrKeyFunc]
    model: NotRequired[str]
    instructions: NotRequired[str]
    modalities: NotRequired[list[Literal["text", "audio"]]]
    voice: NotRequired[str]

    input_audio_format: NotRequired[RealtimeAudioFormat]
    output_audio_format: NotRequired[RealtimeAudioFormat]
    input_audio_transcription: NotRequired[RealtimeInputAudioTranscriptionConfig]
    turn_detection: NotRequired[RealtimeTurnDetectionConfig]

    tool_choice: NotRequired[ToolChoice]
    tools: NotRequired[list[FunctionTool]]


APIKeyOrKeyFunc = str | Callable[[], MaybeAwaitable[str]]
"""Either an API key or a function that returns an API key."""


async def get_api_key(key: APIKeyOrKeyFunc | None) -> str | None:
    """Get the API key from the key or key function."""
    if key is None:
        return None
    elif isinstance(key, str):
        return key

    result = key()
    if inspect.isawaitable(result):
        return await result
    return result

    # TODO (rm) Add tracing support
    # tracing: NotRequired[RealtimeTracingConfig | None]
