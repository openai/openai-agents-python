from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any, overload

from .._config_coercion import _declared_dataclass_type, coerce_dataclass_config
from ..tracing import TracingConfig
from ..tracing.util import gen_group_id
from .model import STTModelSettings, TTSModelSettings, VoiceModelProvider
from .models.openai_model_provider import OpenAIVoiceModelProvider


def _preserve_legacy_positional_arguments(
    cls: type[VoicePipelineConfig],
) -> type[VoicePipelineConfig]:
    generated_init = cls.__init__

    @wraps(generated_init)
    def init(self: VoicePipelineConfig, *args: Any, **kwargs: Any) -> None:
        # Before tracing was added, the third positional argument was the text privacy flag.
        # Insert only that slot so the dataclass still owns defaults and argument validation.
        if len(args) >= 3 and isinstance(args[2], bool):
            args = (*args[:2], kwargs.pop("tracing", None), *args[2:])
        generated_init(self, *args, **kwargs)

    cls.__init__ = init  # type: ignore[method-assign]
    return cls


@_preserve_legacy_positional_arguments
@dataclass
class VoicePipelineConfig:
    """Configuration for a `VoicePipeline`."""

    model_provider: VoiceModelProvider = field(default_factory=OpenAIVoiceModelProvider)
    """The voice model provider to use for the pipeline. Defaults to OpenAI."""

    tracing_disabled: bool = False
    """Whether to disable tracing of the pipeline, including when a caller trace is active.
    Defaults to `False`. The caller's trace and span remain active outside the pipeline.
    """

    tracing: TracingConfig | None = None
    """Tracing configuration for this pipeline."""

    trace_include_sensitive_data: bool = True
    """Whether to include sensitive data in traces. Defaults to `True`. This is specifically for the
      voice pipeline, and not for anything that goes on inside your Workflow."""

    trace_include_sensitive_audio_data: bool = True
    """Whether to include audio data in traces. Defaults to `True`."""

    workflow_name: str = "Voice Agent"
    """The name of the workflow to use for tracing. Defaults to `Voice Agent`."""

    group_id: str = field(default_factory=gen_group_id)
    """
    A grouping identifier to use for tracing, to link multiple traces from the same conversation
    or process. If not provided, we will create a random group ID.
    """

    trace_metadata: dict[str, Any] | None = None
    """
    An optional dictionary of additional metadata to include with the trace.
    """

    stt_settings: STTModelSettings = field(default_factory=STTModelSettings)
    """The settings to use for the STT model."""

    tts_settings: TTSModelSettings = field(default_factory=TTSModelSettings)
    """The settings to use for the TTS model."""

    if TYPE_CHECKING:

        @overload
        def __init__(
            self,
            model_provider: VoiceModelProvider = ...,
            tracing_disabled: bool = False,
            tracing: TracingConfig | None = None,
            trace_include_sensitive_data: bool = True,
            trace_include_sensitive_audio_data: bool = True,
            workflow_name: str = "Voice Agent",
            group_id: str = ...,
            trace_metadata: dict[str, Any] | None = None,
            stt_settings: STTModelSettings | dict[str, Any] = ...,
            tts_settings: TTSModelSettings | dict[str, Any] = ...,
        ) -> None: ...

        @overload
        def __init__(
            self,
            model_provider: VoiceModelProvider,
            tracing_disabled: bool,
            trace_include_sensitive_data: bool,
            /,
            trace_include_sensitive_audio_data: bool = True,
            workflow_name: str = "Voice Agent",
            group_id: str = ...,
            trace_metadata: dict[str, Any] | None = None,
            stt_settings: STTModelSettings | dict[str, Any] = ...,
            tts_settings: TTSModelSettings | dict[str, Any] = ...,
            *,
            tracing: TracingConfig | None = None,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def __post_init__(self) -> None:
        self.stt_settings = coerce_dataclass_config(
            self.stt_settings,
            _declared_dataclass_type(type(self), "stt_settings", STTModelSettings),
            parameter_name="voice.stt",
        )
        self.tts_settings = coerce_dataclass_config(
            self.tts_settings,
            _declared_dataclass_type(type(self), "tts_settings", TTSModelSettings),
            parameter_name="voice.tts",
        )
