"""Typed public Realtime settings, serialized without introducing defaults."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from typing_extensions import Required, TypedDict

from ._public import preserve_module_identity as _preserve_module_identity


class NativeRealtimeTranscription(TypedDict, total=False):
    model: str
    language: str
    prompt: str
    keywords: list[str]
    languages: list[str]
    delay: Literal["minimal", "low", "medium", "high", "xhigh"]


class NativeRealtimeNoiseReduction(TypedDict):
    type: Literal["near_field", "far_field"]


class NativeRealtimeServerVad(TypedDict, total=False):
    type: Required[Literal["server_vad"]]
    create_response: bool
    interrupt_response: bool
    threshold: float
    prefix_padding_ms: int
    silence_duration_ms: int
    idle_timeout_ms: int
    model_version: str


class NativeRealtimeSemanticVad(TypedDict, total=False):
    type: Required[Literal["semantic_vad"]]
    create_response: bool
    interrupt_response: bool
    eagerness: Literal["auto", "low", "medium", "high"]
    model_version: str


class NativeRealtimeCustomVoice(TypedDict):
    id: str


class NativeRealtimePromptText(TypedDict):
    type: Literal["input_text"]
    text: str


class NativeRealtimePromptImage(TypedDict, total=False):
    type: Required[Literal["input_image"]]
    image_url: str
    file_id: str
    detail: Literal["auto", "low", "high", "original"]


class NativeRealtimePromptFile(TypedDict, total=False):
    type: Required[Literal["input_file"]]
    file_url: str
    file_id: str
    file_data: str
    filename: str


class NativeRealtimePrompt(TypedDict, total=False):
    id: Required[str]
    version: str
    variables: dict[
        str,
        str | NativeRealtimePromptText | NativeRealtimePromptImage | NativeRealtimePromptFile,
    ]


class NativeRealtimeTracing(TypedDict, total=False):
    """Provider tracing settings, independent of client SDK span export."""

    workflow_name: str
    group_id: str
    metadata: Any


class NativeRealtimeReasoning(TypedDict, total=False):
    effort: str


class NativeRealtimeMcpToolFilter(TypedDict, total=False):
    tool_names: list[str]
    read_only: bool


class NativeRealtimeMcpApprovalFilter(TypedDict, total=False):
    always: NativeRealtimeMcpToolFilter
    never: NativeRealtimeMcpToolFilter


class NativeRealtimeHostedMcpTool(TypedDict, total=False):
    """Provider-executed tools with exactly one server_url or connector_id.

    These tools use provider-hosted approvals, outside native MCP client hooks.
    """

    server_label: Required[str]
    server_url: str
    connector_id: str
    authorization: str
    server_description: str
    headers: dict[str, str]
    allowed_tools: list[str] | NativeRealtimeMcpToolFilter
    require_approval: Literal["always", "never"] | NativeRealtimeMcpApprovalFilter


class NativeRealtimeToolChoiceFunction(TypedDict):
    type: Literal["function"]
    name: str


NativeRealtimeToolChoice = Literal["auto", "none", "required"] | NativeRealtimeToolChoiceFunction


class NativeRealtimeSettings(TypedDict, total=False):
    """Initial overrides or sparse updates; omission preserves the existing value.

    None explicitly disables nullable settings. Formats configure provider bytes;
    declaring a codec does not convert incoming or outgoing audio samples.
    """

    input_format: Literal["pcm16", "g711_ulaw", "g711_alaw"]
    output_format: Literal["pcm16", "g711_ulaw", "g711_alaw"]
    noise_reduction: NativeRealtimeNoiseReduction | None
    transcription: NativeRealtimeTranscription | None
    turn_detection: NativeRealtimeServerVad | NativeRealtimeSemanticVad | None
    output_voice: str | NativeRealtimeCustomVoice
    output_speed: float
    prompt: NativeRealtimePrompt | None
    tracing: Literal["auto"] | NativeRealtimeTracing | None
    max_output_tokens: int | Literal["inf"]
    parallel_tool_calls: bool
    tool_choice: NativeRealtimeToolChoice
    reasoning: NativeRealtimeReasoning
    hosted_mcp_tools: list[NativeRealtimeHostedMcpTool]


class NativeRealtimeSessionUpdate(NativeRealtimeSettings, total=False):
    """Named changes to one generation; provider mutability rules still apply."""

    model: str
    instructions: str
    output_modality: Literal["audio", "text"]


class NativeRealtimeResponseOptions(TypedDict, total=False):
    """Provider overrides for one response within the accepted session."""

    instructions: str
    output_modality: Literal["audio", "text"]
    max_output_tokens: int | Literal["inf"]


def serialize_realtime_settings(settings: NativeRealtimeSettings) -> dict[str, Any]:
    """Copy canonical settings without filling omitted fields or coercing NaN."""
    if "tool_choice" in settings:
        choice = settings["tool_choice"]
        if not (
            isinstance(choice, str)
            and choice in ("auto", "none", "required")
            or isinstance(choice, dict)
            and set(choice) == {"type", "name"}
            and choice.get("type") == "function"
            and isinstance(choice.get("name"), str)
        ):
            raise ValueError("tool_choice must be auto, none, required, or a named function")
    return cast(dict[str, Any], json.loads(json.dumps(settings, allow_nan=False)))


def serialize_realtime_session_update(update: NativeRealtimeSessionUpdate) -> dict[str, Any]:
    """Copy a sparse canonical update; this is not a provider event envelope."""
    return serialize_realtime_settings(update)


_preserve_module_identity(globals(), "agents.realtime")
