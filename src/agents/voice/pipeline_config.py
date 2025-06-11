from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from ..tracing.util import gen_group_id
from .model import STTModelSettings, TTSModelSettings, VoiceModelProvider
from .models.openai_model_provider import OpenAIVoiceModelProvider


@dataclass
class VoicePipelineConfig:
    """Configuration for a `VoicePipeline`."""

    model_provider: VoiceModelProvider = field(default_factory=OpenAIVoiceModelProvider)
    """The voice model provider to use for the pipeline. Defaults to OpenAI."""

    tracing_disabled: bool = False
    """Whether to disable tracing of the pipeline. Defaults to `False`."""

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

    # Settings for the new real-time pipeline
    realtime_settings: dict[str, Any] = field(default_factory=dict)
    """
    Settings specific to the RealtimeVoicePipeline. Can include things like:
    - "system_message": str
    - "assistant_voice": str (e.g., "alloy")
    - Future client-side VAD thresholds or other model-specific params.
    Example: {"system_message": "You are a helpful assistant.", "assistant_voice": "echo"}
    """

    def __init__(
        self,
        *,
        trace_id: str | None = None,
        trace_config: Dict[str, Any] | None = None,
        upload_audio_data: bool = False,
        trace_disable: bool = False,
        model_provider: VoiceModelProvider | None = None,
        tts_settings: Dict[str, Any] | None = None,
        stt_settings: Dict[str, Any] | None = None,
        realtime_settings: Dict[str, Any] | None = None,
        group_id: str | None = None,
    ):
        """Initialize the voice pipeline configuration.

        Args:
            trace_id: The trace ID to use for the trace. If None, one will be generated.
            trace_config: The trace configuration to use for the trace.
            upload_audio_data: Whether to upload audio data to the trace.
            trace_disable: Whether to disable tracing.
            model_provider: The model provider to use for the pipeline.
            tts_settings: The settings to use for TTS models.
            stt_settings: The settings to use for STT models.
            realtime_settings: The settings to use for realtime models.
            group_id: The grouping identifier to use for tracing.
        """
        self.trace_id = trace_id
        self.trace_config = trace_config
        self.upload_audio_data = upload_audio_data
        self.trace_disable = trace_disable
        self.group_id = group_id or gen_group_id()

        # Normalise STT / TTS settings into their dataclass types
        if isinstance(stt_settings, STTModelSettings):
            self.stt_settings = stt_settings
        else:  # dict or None
            self.stt_settings = STTModelSettings(**(stt_settings or {}))

        if isinstance(tts_settings, TTSModelSettings):
            self.tts_settings = tts_settings
        else:
            self.tts_settings = TTSModelSettings(**(tts_settings or {}))

        # Initialize realtime settings with defaults if not provided
        self._realtime_settings = {
            # Default to server-side voice activity detection (VAD)
            "turn_detection": "server_vad",
            # Default voice for audio responses
            "assistant_voice": "alloy",
        }

        # Update with any provided settings
        if realtime_settings:
            self._realtime_settings.update(realtime_settings)

        # Backwards-compat: honour trace_disable flag but keep original attribute name
        self.tracing_disabled = trace_disable

        # Ensure model_provider is always a valid provider instance
        self.model_provider = model_provider or OpenAIVoiceModelProvider()

    @property
    def realtime_settings(self) -> Dict[str, Any]:
        """Get the realtime configuration settings.

        Returns:
            A dictionary of realtime settings.
        """
        return self._realtime_settings
