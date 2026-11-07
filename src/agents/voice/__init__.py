"""Optional speech pipelines with native Session execution.

Speech input, synthesis and lifecycle types reuse the retained speech engine.
Native speech begins only after output validation and completion acceptance.
Install the ``voice`` extra from the matching SDK source or preview wheel.
Realtime speech agents remain available through ``agents.classic.realtime``;
use an ordinary function tool to delegate their work to this native workflow.
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
