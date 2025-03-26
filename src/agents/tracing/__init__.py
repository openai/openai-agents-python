import atexit

from .create import (
    agent_span,
    custom_span,
    function_span,
    generation_span,
    get_current_span,
    get_current_trace,
    guardrail_span,
    handoff_span,
    mcp_tools_span,
    response_span,
    speech_group_span,
    speech_span,
    trace,
    transcription_span,
)
from .processor_interface import TracingProcessor
from .processors import default_exporter, default_processor
from .setup import GLOBAL_TRACE_PROVIDER
from .span_data import (
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    MCPListToolsSpanData,
    ResponseSpanData,
    SpanData,
    SpeechGroupSpanData,
    SpeechSpanData,
    TranscriptionSpanData,
)
from .spans import Span, SpanError
from .traces import Trace
from .util import gen_span_id, gen_trace_id

__all__ = [
    "add_trace_processor",
    "agent_span",
    "custom_span",
    "function_span",
    "generation_span",
    "get_current_span",
    "get_current_trace",
    "guardrail_span",
    "handoff_span",
    "response_span",
    "set_trace_processors",
    "set_tracing_disabled",
    "trace",
    "Trace",
    "SpanError",
    "Span",
    "SpanData",
    "AgentSpanData",
    "CustomSpanData",
    "FunctionSpanData",
    "GenerationSpanData",
    "GuardrailSpanData",
    "HandoffSpanData",
    "MCPListToolsSpanData",
    "ResponseSpanData",
    "SpeechGroupSpanData",
    "SpeechSpanData",
    "TranscriptionSpanData",
    "TracingProcessor",
    "gen_trace_id",
    "gen_span_id",
    "speech_group_span",
    "speech_span",
    "transcription_span",
    "mcp_tools_span",
]


def add_trace_processor(span_processor: TracingProcessor) -> None:
    """
    Adds a new trace processor. This processor will receive all traces/spans.
    """
    GLOBAL_TRACE_PROVIDER.register_processor(span_processor)


def set_trace_processors(processors: list[TracingProcessor]) -> None:
    """
    Set the list of trace processors. This will replace the current list of processors.
    """
    GLOBAL_TRACE_PROVIDER.set_processors(processors)


def set_tracing_disabled(disabled: bool) -> None:
    """
    Set whether tracing is globally disabled.
    """
    GLOBAL_TRACE_PROVIDER.set_disabled(disabled)


def set_tracing_export_api_key(api_key: str) -> None:
    """
    Set the OpenAI API key for the backend exporter.
    """
    default_exporter().set_api_key(api_key)


# Add the default processor, which exports traces and spans to the backend in batches. You can
# change the default behavior by either:
# 1. calling add_trace_processor(), which adds additional processors, or
# 2. calling set_trace_processors(), which replaces the default processor.
add_trace_processor(default_processor())

atexit.register(GLOBAL_TRACE_PROVIDER.shutdown)
