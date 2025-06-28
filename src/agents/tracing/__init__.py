"""
OpenAI Agents Tracing

Configuration:
  - AGENT_TRACE_PROVIDER: Optional "module.ClassName" specifying a custom TraceProvider.
    If unset, DefaultTraceProvider is used.
  - Use `set_tracing_export_api_key()` to configure the API key for the built-in exporter.

Runtime APIs:
  - add_trace_processor(span_processor: TracingProcessor)
  - set_trace_processors(processors: List[TracingProcessor])
  - set_tracing_disabled(disabled: bool)
  - set_tracing_export_api_key(api_key: str)
"""

# Enable postponed evaluation of annotations for Python <3.10 compatibility
from __future__ import annotations

import atexit
import os
import logging
from typing import List

# Critical imports explicitly exposed in __all__
from .provider import TraceProvider, DefaultTraceProvider
from .setup import set_trace_provider, get_trace_provider
from .processor_interface import TracingProcessor
from .span_data import (
    AgentSpanData, CustomSpanData, FunctionSpanData, GenerationSpanData,
    GuardrailSpanData, HandoffSpanData, MCPListToolsSpanData,
    ResponseSpanData, SpanData, SpeechGroupSpanData, SpeechSpanData,
    TranscriptionSpanData
)
from .spans import Span, SpanError
from .traces import Trace
from .util import gen_span_id, gen_trace_id
from .create import (
    agent_span, custom_span, function_span, generation_span, get_current_span,
    get_current_trace, guardrail_span, handoff_span, mcp_tools_span,
    response_span, speech_group_span, speech_span, trace, transcription_span
)

__all__ = [
    "TraceProvider",
    "DefaultTraceProvider",
    "set_trace_provider",
    "get_trace_provider",
    "add_trace_processor",
    "set_trace_processors",
    "set_tracing_disabled",
    "set_tracing_export_api_key",
    "agent_span", "custom_span", "function_span", "generation_span",
    "get_current_span", "get_current_trace",
    "guardrail_span", "handoff_span", "response_span", "speech_group_span",
    "speech_span", "trace", "transcription_span", "mcp_tools_span",
    "Trace", "Span", "SpanError",
    "SpanData", "AgentSpanData", "CustomSpanData", "FunctionSpanData",
    "GenerationSpanData", "GuardrailSpanData", "HandoffSpanData",
    "MCPListToolsSpanData", "ResponseSpanData", "SpeechGroupSpanData",
    "SpeechSpanData", "TranscriptionSpanData",
    "TracingProcessor", "gen_trace_id", "gen_span_id",
]

def _load_provider() -> TraceProvider:
    """
    Load a TraceProvider implementation based on AGENT_TRACE_PROVIDER env var,
    or fall back to DefaultTraceProvider.
    """
    from .provider import DefaultTraceProvider, TraceProvider

    spec = os.getenv("AGENT_TRACE_PROVIDER")
    if not spec:
        provider = DefaultTraceProvider()
        logging.debug("Using default trace provider: %s", type(provider).__name__)
        return provider

    module_name, _, class_name = spec.rpartition(".")
    try:
        mod = __import__(module_name, fromlist=[class_name])
        provider_cls = getattr(mod, class_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Custom provider '{spec}' not found. Check your AGENT_TRACE_PROVIDER value."
        ) from exc
    except Exception as exc:
        logging.warning(
            "Error loading AGENT_TRACE_PROVIDER=%r: %s", spec, exc
        )
        return DefaultTraceProvider()

    if not issubclass(provider_cls, TraceProvider):
        raise TypeError(f"{spec!r} must inherit from TraceProvider")

    provider = provider_cls()
    logging.debug("Using custom trace provider: %s", spec)
    return provider

# Bootstrap the global provider
try:
    provider = _load_provider()
    set_trace_provider(provider)
except Exception as exc:
    logging.warning("Failed to set trace provider: %s", exc)

def add_trace_processor(span_processor: TracingProcessor) -> None:
    """Register a new trace processor to receive all spans."""
    try:
        from .setup import get_trace_provider as _get
        _get().register_processor(span_processor)
    except Exception as exc:
        logging.warning("Failed to register trace processor: %s", exc)

def set_trace_processors(processors: List[TracingProcessor]) -> None:
    """Replace all trace processors with the given list."""
    if not processors:
        logging.warning("Empty processor list provided to set_trace_processors()")
    try:
        from .setup import get_trace_provider as _get
        _get().set_processors(processors)
    except Exception as exc:
        logging.warning("Failed to set trace processors: %s", exc)

def set_tracing_disabled(disabled: bool) -> None:
    """Globally enable or disable tracing at runtime."""
    try:
        from .setup import get_trace_provider as _get
        _get().set_disabled(disabled)
    except Exception as exc:
        logging.warning("Failed to toggle tracing: %s", exc)

def set_tracing_export_api_key(api_key: str) -> None:
    """Configure the OpenAI API key for the built-in tracing exporter."""
    try:
        from .processors import default_exporter
        exporter = default_exporter()
        if exporter is None:
            raise RuntimeError(
                "No tracing exporter available; did you install the optional extras?"
            )
        exporter.set_api_key(api_key)
    except Exception as exc:
        logging.warning("Failed to set tracing export API key: %s", exc)
        if isinstance(exc, RuntimeError):
            raise

# Install default processor (only if it succeeds)
try:
    from .processors import default_processor
    proc = default_processor()
    if proc is not None:
        add_trace_processor(proc)
except Exception as exc:
    logging.warning("Failed to install default trace processor: %s", exc)

# Ensure provider shutdown at exit (with safety checks)
try:
    provider = get_trace_provider()
    shutdown_fn = getattr(provider, "shutdown", None)
    if callable(shutdown_fn):
        atexit.register(shutdown_fn)
    else:
        logging.debug("Trace provider has no shutdown method - skipping cleanup registration")
except Exception as exc:
    logging.warning("Failed to register shutdown hook: %s", exc)
