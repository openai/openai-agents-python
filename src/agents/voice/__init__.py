"""Optional speech pipelines with native Session execution.

Speech input, synthesis and lifecycle types reuse the retained speech engine.
By default, native speech begins only after output validation and completion
acceptance. Set ``NativeSessionVoiceWorkflow(..., early_speech=True)`` to stream
text before validation. Playback can include text that later fails validation.
Install the ``voice`` extra from the matching SDK source or preview wheel.
For a native two-way speech session, use ``Session.realtime()`` and the types
in ``agents.realtime``.
"""

from ..classic.voice import (
    AudioInput,
    OpenAISTTModel,
    OpenAISTTTranscriptionSession,
    OpenAITTSModel,
    OpenAIVoiceModelProvider,
    StreamedAudioInput,
    StreamedAudioResult,
    StreamedTranscriptionSession,
    STTModel,
    STTModelSettings,
    STTWebsocketConnectionError,
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
    "OpenAISTTTranscriptionSession",
    "OpenAITTSModel",
    "OpenAIVoiceModelProvider",
    "STTModel",
    "STTModelSettings",
    "STTWebsocketConnectionError",
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
