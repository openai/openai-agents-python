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


class RealtimeClientMessage(TypedDict):
    """A raw message to be sent to the model."""

    type: str  # explicitly required
    other_data: NotRequired[dict[str, Any]]
    """Merged into the message body."""


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


class RealtimeSessionModelSettings(TypedDict):
    """Model settings for a realtime model session."""

    model_name: NotRequired[RealtimeModelName]

    instructions: NotRequired[str]
    modalities: NotRequired[list[Literal["text", "audio"]]]
    voice: NotRequired[str]

    input_audio_format: NotRequired[RealtimeAudioFormat]
    output_audio_format: NotRequired[RealtimeAudioFormat]
    input_audio_transcription: NotRequired[RealtimeInputAudioTranscriptionConfig]
    turn_detection: NotRequired[RealtimeTurnDetectionConfig]

    tool_choice: NotRequired[ToolChoice]
    tools: NotRequired[list[Tool]]
    handoffs: NotRequired[list[Handoff]]

    tracing: NotRequired[RealtimeModelTracingConfig | None]


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
    model_settings: NotRequired[RealtimeSessionModelSettings]

    output_guardrails: NotRequired[list[OutputGuardrail[Any]]]
    """List of output guardrails to run on the agent's responses."""

    guardrails_settings: NotRequired[RealtimeGuardrailsSettings]
    """Settings for guardrail execution."""

    tracing_disabled: NotRequired[bool]
    """Whether tracing is disabled for this run."""

    # TODO (rm) Add history audio storage config


class RealtimeUserInputText(TypedDict):
    type: Literal["input_text"]
    text: str


class RealtimeUserInputMessage(TypedDict):
    type: Literal["message"]
    role: Literal["user"]
    content: list[RealtimeUserInputText]


RealtimeUserInput: TypeAlias = Union[str, RealtimeUserInputMessage]
