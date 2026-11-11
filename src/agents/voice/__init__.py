"""Speech pipelines with explicit native and retained Classic execution.

Speech input, synthesis and lifecycle types reuse the retained speech engine.
``SingleAgentVoiceWorkflow`` has been retaining Classic Agent/Runner execution;
``NativeSessionVoiceWorkflow`` has been using a caller-owned native Session.
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
    SingleAgentVoiceWorkflow,
    SingleAgentWorkflowCallbacks,
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
    VoiceWorkflowHelper,
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
    "SingleAgentVoiceWorkflow",
    "SingleAgentWorkflowCallbacks",
    "VoiceWorkflowHelper",
    "get_sentence_based_splitter",
]
