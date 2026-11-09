"""Optional speech pipelines with native Session execution.

Speech input, synthesis and lifecycle types reuse the retained speech engine.
Native speech begins only after output validation and completion acceptance.
Install the ``voice`` extra from the matching SDK source or preview wheel.
For a native two-way speech session, use ``Session.realtime()`` and the types
in ``agents.realtime``.
"""

from ..classic.voice import (
    AudioInput,
    OpenAISTTModel,
    OpenAITTSModel,
    OpenAIVoiceModelProvider,
    StreamedAudioInput,
    StreamedAudioResult,
    StreamedTranscriptionSession,
    STTModel,
    STTModelSettings,
    TTSCustomVoice,
    TTSModel,
    TTSModelSettings,
    TTSVoice,
    VoiceModelProvider,
    VoicePipeline,
    VoicePipelineConfig,
    VoiceStreamEvent,
    VoiceStreamEventAudio,
    VoiceStreamEventError,
    VoiceStreamEventLifecycle,
    VoiceWorkflowBase,
    get_sentence_based_splitter,
)
from .workflow import NativeSessionVoiceWorkflow

__all__ = [
    "AudioInput",
    "NativeSessionVoiceWorkflow",
    "OpenAISTTModel",
    "OpenAITTSModel",
    "OpenAIVoiceModelProvider",
    "STTModel",
    "STTModelSettings",
    "StreamedAudioInput",
    "StreamedAudioResult",
    "StreamedTranscriptionSession",
    "TTSCustomVoice",
    "TTSModel",
    "TTSModelSettings",
    "TTSVoice",
    "VoiceModelProvider",
    "VoicePipeline",
    "VoicePipelineConfig",
    "VoiceStreamEvent",
    "VoiceStreamEventAudio",
    "VoiceStreamEventError",
    "VoiceStreamEventLifecycle",
    "VoiceWorkflowBase",
    "get_sentence_based_splitter",
]
