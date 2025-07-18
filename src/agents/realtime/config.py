from __future__ import annotations

from typing import (
    Any,
    Literal,
    Union,
)

from typing_extensions import NotRequired, TypeAlias, TypedDict

from ..guardrail import OutputGuardrail
from ..handoffs import Handoff
from ..model_settings import ToolChoice
from ..tool import Tool

RealtimeModelName: TypeAlias = Union[
    Literal[
        "gpt-4o-realtime-preview",
        "gpt-4o-mini-realtime-preview",
        "gpt-4o-realtime-preview-2025-06-03",
        "gpt-4o-realtime-preview-2024-12-17",
        "gpt-4o-realtime-preview-2024-10-01",
        "gpt-4o-mini-realtime-preview-2024-12-17",
    ],
    str,
]
"""The name of a realtime model."""


RealtimeAudioFormat: TypeAlias = Union[Literal["pcm16", "g711_ulaw", "g711_alaw"], str]
"""The audio format for realtime audio streams."""


class RealtimeClientMessage(TypedDict):
    """A raw message to be sent to the model."""

    type: str  # explicitly required
    """The type of the message."""

    other_data: NotRequired[dict[str, Any]]
    """Merged into the message body."""


class RealtimeInputAudioTranscriptionConfig(TypedDict):
    """Configuration for audio transcription in realtime sessions."""

    language: NotRequired[str]
    """The language code for transcription."""

    model: NotRequired[Literal["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"] | str]
    """The transcription model to use."""

    prompt: NotRequired[str]
    """An optional prompt to guide transcription."""


class RealtimeTurnDetectionConfig(TypedDict):
    """Turn detection config. Allows extra vendor keys if needed."""

    type: NotRequired[Literal["semantic_vad", "server_vad"]]
    """The type of voice activity detection to use."""

    create_response: NotRequired[bool]
    """Whether to create a response when a turn is detected."""

    eagerness: NotRequired[Literal["auto", "low", "medium", "high"]]
    """How eagerly to detect turn boundaries."""

    interrupt_response: NotRequired[bool]
    """Whether to allow interrupting the assistant's response."""

    prefix_padding_ms: NotRequired[int]
    """Padding time in milliseconds before turn detection."""

    silence_duration_ms: NotRequired[int]
    """Duration of silence in milliseconds to trigger turn detection."""

    threshold: NotRequired[float]
    """The threshold for voice activity detection."""


class RealtimeSessionModelSettings(TypedDict):
    """Model settings for a realtime model session."""

    model_name: NotRequired[RealtimeModelName]
    """The name of the realtime model to use."""

    instructions: NotRequired[str]
    """System instructions for the model."""

    modalities: NotRequired[list[Literal["text", "audio"]]]
    """The modalities the model should support."""

    voice: NotRequired[str]
    """The voice to use for audio output."""

    input_audio_format: NotRequired[RealtimeAudioFormat]
    """The format for input audio streams."""

    output_audio_format: NotRequired[RealtimeAudioFormat]
    """The format for output audio streams."""

    input_audio_transcription: NotRequired[RealtimeInputAudioTranscriptionConfig]
    """Configuration for transcribing input audio."""

    turn_detection: NotRequired[RealtimeTurnDetectionConfig]
    """Configuration for detecting conversation turns."""

    tool_choice: NotRequired[ToolChoice]
    """How the model should choose which tools to call."""

    tools: NotRequired[list[Tool]]
    """List of tools available to the model."""

    handoffs: NotRequired[list[Handoff]]
    """List of handoff configurations."""

    tracing: NotRequired[RealtimeModelTracingConfig | None]
    """Configuration for request tracing."""


class RealtimeGuardrailsSettings(TypedDict):
    """Settings for output guardrails in realtime sessions."""

    debounce_text_length: NotRequired[int]
    """
    The minimum number of characters to accumulate before running guardrails on transcript
    deltas. Defaults to 100. Guardrails run every time the accumulated text reaches
    1x, 2x, 3x, etc. times this threshold.
    """


class RealtimeModelTracingConfig(TypedDict):
    """Configuration for tracing in realtime model sessions."""

    workflow_name: NotRequired[str]
    """The workflow name to use for tracing."""

    group_id: NotRequired[str]
    """A group identifier to use for tracing, to link multiple traces together."""

    metadata: NotRequired[dict[str, Any]]
    """Additional metadata to include with the trace."""


class RealtimeRunConfig(TypedDict):
    """Configuration for running a realtime agent session."""

    model_settings: NotRequired[RealtimeSessionModelSettings]
    """Settings for the realtime model session."""

    output_guardrails: NotRequired[list[OutputGuardrail[Any]]]
    """List of output guardrails to run on the agent's responses."""

    guardrails_settings: NotRequired[RealtimeGuardrailsSettings]
    """Settings for guardrail execution."""

    tracing_disabled: NotRequired[bool]
    """Whether tracing is disabled for this run."""

    # TODO (rm) Add history audio storage config


class RealtimeUserInputText(TypedDict):
    """A text input from the user."""

    type: Literal["input_text"]
    """The type identifier for text input."""

    text: str
    """The text content from the user."""


class RealtimeUserInputMessage(TypedDict):
    """A message input from the user."""

    type: Literal["message"]
    """The type identifier for message inputs."""

    role: Literal["user"]
    """The role identifier for user messages."""

    content: list[RealtimeUserInputText]
    """List of text content items in the message."""


RealtimeUserInput: TypeAlias = Union[str, RealtimeUserInputMessage]
"""User input that can be a string or structured message."""
