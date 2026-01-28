"""OpenTelemetry tracing processor for the OpenAI Agents SDK.

This module provides an OpenTelemetry-based TracingProcessor that bridges
the Agents SDK's tracing system to OpenTelemetry, enabling export to any
OTLP-compatible backend (Jaeger, Datadog, Honeycomb, Grafana Tempo, etc.).

The processor maps SDK trace and span types to OpenTelemetry spans following
the OpenTelemetry Semantic Conventions for Generative AI systems where applicable.

See: https://opentelemetry.io/docs/specs/semconv/gen-ai/

Example:
    ```python
    from agents import add_trace_processor
    from agents.extensions.tracing import OpenTelemetryTracingProcessor

    # Create and register the processor
    otel_processor = OpenTelemetryTracingProcessor()
    add_trace_processor(otel_processor)

    # Now all agent traces will be exported to your configured OTel backend
    result = await Runner.run(agent, "Hello!")
    ```

Requirements:
    pip install opentelemetry-api opentelemetry-sdk
    # Or: pip install openai-agents[opentelemetry]
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any

from agents.tracing import TracingProcessor

if TYPE_CHECKING:
    from agents.tracing.spans import Span as AgentSpan
    from agents.tracing.traces import Trace as AgentTrace

logger = logging.getLogger(__name__)

# Default tracer name for agent spans
DEFAULT_TRACER_NAME = "openai.agents"

# Attribute key prefixes following OTel semantic conventions
_ATTR_PREFIX_GEN_AI = "gen_ai"
_ATTR_PREFIX_AGENT = "agent"


def _try_import_opentelemetry() -> tuple[Any, Any, Any, Any, Any]:
    """Try to import OpenTelemetry dependencies.

    Returns:
        Tuple of (trace module, SpanKind, Status, StatusCode, Context)

    Raises:
        ImportError: If opentelemetry packages are not installed.
    """
    try:
        from opentelemetry import context as otel_context, trace
        from opentelemetry.trace import SpanKind, Status, StatusCode
    except ImportError as e:
        raise ImportError(
            "OpenTelemetry packages are required for OpenTelemetryTracingProcessor. "
            "Install them with: pip install opentelemetry-api opentelemetry-sdk "
            "or pip install openai-agents[opentelemetry]"
        ) from e
    return trace, SpanKind, Status, StatusCode, otel_context


class OpenTelemetryTracingProcessor(TracingProcessor):
    """A TracingProcessor that exports traces to OpenTelemetry.

    This processor receives trace and span events from the Agents SDK and
    creates corresponding OpenTelemetry spans. It maintains the parent-child
    relationships between spans and maps SDK-specific data to OTel attributes.

    The processor is thread-safe and can be used in concurrent environments.

    Attributes:
        tracer_name: The name of the OTel tracer (default: "openai.agents").

    Example:
        ```python
        from agents import add_trace_processor, set_trace_processors
        from agents.extensions.tracing import OpenTelemetryTracingProcessor

        # Option 1: Add alongside default OpenAI backend processor
        processor = OpenTelemetryTracingProcessor()
        add_trace_processor(processor)

        # Option 2: Replace default processor (traces only go to OTel)
        processor = OpenTelemetryTracingProcessor()
        set_trace_processors([processor])
        ```
    """

    def __init__(
        self,
        tracer_name: str = DEFAULT_TRACER_NAME,
    ) -> None:
        """Initialize the OpenTelemetry tracing processor.

        Args:
            tracer_name: Name of the tracer to use. Defaults to "openai.agents".
        """
        trace, SpanKind, Status, StatusCode, otel_context = _try_import_opentelemetry()

        self._trace = trace
        self._SpanKind = SpanKind
        self._Status = Status
        self._StatusCode = StatusCode
        self._otel_context = otel_context

        self._tracer_name = tracer_name
        self._tracer = trace.get_tracer(tracer_name)

        # Lock for thread-safe access to span tracking dictionaries
        self._lock = threading.Lock()

        # Map SDK trace/span IDs to OTel spans for parent-child relationships.
        # We store only the OTel Span (not context tokens) because we do NOT attach
        # spans to the global context. This avoids out-of-order detach issues when
        # SDK spans overlap (e.g., parallel tool calls).
        self._active_spans: dict[str, Any] = {}
        self._trace_root_spans: dict[str, Any] = {}

    def on_trace_start(self, trace: AgentTrace) -> None:
        """Handle SDK trace start by creating an OTel root span.

        Args:
            trace: The SDK trace that started.

        Note:
            We do NOT attach the root span to the global OpenTelemetry context.
            Instead, child spans explicitly reference their parent via the span ID
            lookup in _trace_root_spans. This avoids context ordering issues when
            multiple traces or spans overlap.
        """
        try:
            trace_id = trace.trace_id
            workflow_name = trace.name

            # Create root span for this workflow/trace
            span = self._tracer.start_span(
                name=f"workflow: {workflow_name}",
                kind=self._SpanKind.INTERNAL,
                attributes={
                    f"{_ATTR_PREFIX_AGENT}.workflow.name": workflow_name,
                    f"{_ATTR_PREFIX_AGENT}.trace_id": trace_id,
                },
            )

            # Add optional trace metadata
            group_id = getattr(trace, "group_id", None)
            if group_id:
                span.set_attribute(f"{_ATTR_PREFIX_AGENT}.group_id", group_id)

            metadata = getattr(trace, "metadata", None)
            if metadata and isinstance(metadata, dict):
                for key, value in metadata.items():
                    attr_key = f"{_ATTR_PREFIX_AGENT}.metadata.{key}"
                    span.set_attribute(attr_key, _safe_attribute_value(value))

            with self._lock:
                self._trace_root_spans[trace_id] = span

            logger.debug(f"Started OTel span for trace: {trace_id} ({workflow_name})")

        except Exception as e:
            logger.error(f"Failed to create OTel span for trace start: {e}")

    def on_trace_end(self, trace: AgentTrace) -> None:
        """Handle SDK trace end by closing the OTel root span.

        Args:
            trace: The SDK trace that ended.
        """
        trace_id = trace.trace_id
        otel_span = None

        try:
            with self._lock:
                otel_span = self._trace_root_spans.pop(trace_id, None)

            if otel_span:
                otel_span.set_status(self._Status(self._StatusCode.OK))
                logger.debug(f"Ended OTel span for trace: {trace_id}")
            else:
                logger.warning(f"No OTel span found for trace end: {trace_id}")

        except Exception as e:
            logger.error(f"Failed to process OTel span for trace end: {e}")

        finally:
            # Always end the span, even if processing failed
            if otel_span:
                try:
                    otel_span.end()
                except Exception as e:
                    logger.error(f"Failed to end OTel trace span: {e}")

    def on_span_start(self, span: AgentSpan[Any]) -> None:
        """Handle SDK span start by creating a child OTel span.

        Args:
            span: The SDK span that started.

        Note:
            We do NOT attach spans to the global OpenTelemetry context because SDK spans
            can overlap (e.g., parallel tool calls). Attaching would require LIFO detach
            order, which the SDK does not guarantee. Instead, we explicitly pass parent
            context when creating child spans, avoiding global context manipulation.
        """
        try:
            span_id = span.span_id
            trace_id = span.trace_id
            parent_id = span.parent_id
            span_data = span.span_data

            # Determine the parent context by looking up the parent span explicitly.
            # We do NOT use the global context to avoid issues with overlapping spans.
            parent_context = None
            with self._lock:
                if parent_id and parent_id in self._active_spans:
                    parent_span = self._active_spans[parent_id]
                    parent_context = self._trace.set_span_in_context(parent_span)
                elif trace_id in self._trace_root_spans:
                    parent_span = self._trace_root_spans[trace_id]
                    parent_context = self._trace.set_span_in_context(parent_span)

            # Map span data to OTel span name and attributes
            otel_span_name, attributes, span_kind = self._map_span_data(span_data)

            # Add common attributes
            attributes[f"{_ATTR_PREFIX_AGENT}.span_id"] = span_id
            attributes[f"{_ATTR_PREFIX_AGENT}.trace_id"] = trace_id
            if parent_id:
                attributes[f"{_ATTR_PREFIX_AGENT}.parent_span_id"] = parent_id

            # Create the OTel span with explicit parent context (not global context)
            otel_span = self._tracer.start_span(
                name=otel_span_name,
                context=parent_context,
                kind=span_kind,
                attributes=attributes,
            )

            with self._lock:
                self._active_spans[span_id] = otel_span

            logger.debug(f"Started OTel span: {otel_span_name} ({span_id})")

        except Exception as e:
            logger.error(f"Failed to create OTel span for span start: {e}")

    def on_span_end(self, span: AgentSpan[Any]) -> None:
        """Handle SDK span end by closing the OTel span.

        Args:
            span: The SDK span that ended.
        """
        span_id = span.span_id
        otel_span = None

        try:
            with self._lock:
                otel_span = self._active_spans.pop(span_id, None)

            if not otel_span:
                logger.warning(f"No OTel span found for span end: {span_id}")
                return

            # Update span with final data (e.g., usage metrics, outputs)
            self._update_span_with_final_data(otel_span, span.span_data)

            # Handle errors - use _safe_attribute_value to avoid serialization failures
            error = span.error
            if error:
                error_msg = error.get("message", "Unknown error")
                otel_span.set_status(self._Status(self._StatusCode.ERROR, error_msg))
                otel_span.set_attribute("error.message", error.get("message", ""))
                error_data = error.get("data")
                if error_data:
                    # Use _safe_attribute_value to handle non-serializable data
                    otel_span.set_attribute("error.data", _safe_attribute_value(error_data))
            else:
                otel_span.set_status(self._Status(self._StatusCode.OK))

            logger.debug(f"Ended OTel span: {span_id}")

        except Exception as e:
            logger.error(f"Failed to process OTel span end: {e}")

        finally:
            # Always end the span, even if processing failed
            if otel_span:
                try:
                    otel_span.end()
                except Exception as e:
                    logger.error(f"Failed to end OTel span: {e}")

    def shutdown(self) -> None:
        """Clean up resources and close any unclosed spans."""
        with self._lock:
            # End any spans that weren't properly closed
            for _, span in list(self._active_spans.items()):
                try:
                    span.set_status(
                        self._Status(self._StatusCode.ERROR, "Span not properly closed at shutdown")
                    )
                    span.end()
                except Exception:
                    pass

            for _, span in list(self._trace_root_spans.items()):
                try:
                    span.set_status(
                        self._Status(
                            self._StatusCode.ERROR, "Trace not properly closed at shutdown"
                        )
                    )
                    span.end()
                except Exception:
                    pass

            self._active_spans.clear()
            self._trace_root_spans.clear()

        logger.debug("OpenTelemetry tracing processor shutdown complete")

    def force_flush(self) -> None:
        """Force flush any pending spans.

        Note: OTel spans are exported by the TracerProvider's span processor,
        so we delegate to that if available.
        """
        try:
            provider = self._trace.get_tracer_provider()
            if hasattr(provider, "force_flush"):
                provider.force_flush()
        except Exception as e:
            logger.warning(f"Failed to force flush tracer provider: {e}")

    def _map_span_data(self, span_data: Any) -> tuple[str, dict[str, Any], Any]:
        """Map SDK span data to OTel span name, attributes, and kind.

        Args:
            span_data: The SDK SpanData object.

        Returns:
            Tuple of (span_name, attributes_dict, SpanKind).
        """
        span_type = span_data.type
        attributes: dict[str, Any] = {f"{_ATTR_PREFIX_AGENT}.span.type": span_type}
        kind = self._SpanKind.INTERNAL

        if span_type == "agent":
            return self._map_agent_span(span_data, attributes)

        elif span_type == "generation":
            return self._map_generation_span(span_data, attributes)

        elif span_type == "function":
            return self._map_function_span(span_data, attributes)

        elif span_type == "handoff":
            return self._map_handoff_span(span_data, attributes)

        elif span_type == "guardrail":
            return self._map_guardrail_span(span_data, attributes)

        elif span_type == "custom":
            return self._map_custom_span(span_data, attributes)

        elif span_type == "response":
            return self._map_response_span(span_data, attributes)

        elif span_type == "transcription":
            return self._map_transcription_span(span_data, attributes)

        elif span_type == "speech":
            return self._map_speech_span(span_data, attributes)

        elif span_type == "speech_group":
            return "audio.speech_group", attributes, kind

        elif span_type == "mcp_tools":
            return self._map_mcp_tools_span(span_data, attributes)

        else:
            # Generic fallback for unknown span types
            name = f"agent.{span_type}"
            try:
                exported = span_data.export()
                for key, value in exported.items():
                    if key != "type":
                        attributes[f"span.{key}"] = _safe_attribute_value(value)
            except Exception:
                pass
            return name, attributes, kind

    def _map_agent_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map agent span data to OTel format."""
        name = f"agent: {span_data.name}"
        attributes[f"{_ATTR_PREFIX_AGENT}.name"] = span_data.name

        if span_data.handoffs:
            attributes[f"{_ATTR_PREFIX_AGENT}.handoffs"] = json.dumps(span_data.handoffs)
        if span_data.tools:
            attributes[f"{_ATTR_PREFIX_AGENT}.tools"] = json.dumps(span_data.tools)
        if span_data.output_type:
            attributes[f"{_ATTR_PREFIX_AGENT}.output_type"] = span_data.output_type

        return name, attributes, self._SpanKind.INTERNAL

    def _map_generation_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map generation (LLM call) span data to OTel format.

        Uses OpenTelemetry Semantic Conventions for GenAI where applicable.
        """
        name = "gen_ai.completion"
        kind = self._SpanKind.CLIENT  # LLM call is an outbound request

        if span_data.model:
            attributes[f"{_ATTR_PREFIX_GEN_AI}.request.model"] = span_data.model
            name = f"gen_ai.completion: {span_data.model}"

        if span_data.model_config:
            for key, value in span_data.model_config.items():
                # Skip None/empty values to avoid attribute spam in traces
                if value is not None and value != "":
                    attributes[f"{_ATTR_PREFIX_GEN_AI}.request.{key}"] = _safe_attribute_value(
                        value
                    )

        return name, attributes, kind

    def _map_function_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map function/tool span data to OTel format."""
        name = f"tool: {span_data.name}"
        attributes["tool.name"] = span_data.name

        if span_data.input:
            attributes["tool.input"] = _truncate_string(span_data.input, 4096)
        if span_data.mcp_data:
            attributes["tool.mcp_data"] = json.dumps(span_data.mcp_data)

        return name, attributes, self._SpanKind.INTERNAL

    def _map_handoff_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map handoff span data to OTel format."""
        from_agent = span_data.from_agent or "unknown"
        to_agent = span_data.to_agent or "unknown"
        name = f"handoff: {from_agent} -> {to_agent}"

        attributes[f"{_ATTR_PREFIX_AGENT}.handoff.from"] = from_agent
        attributes[f"{_ATTR_PREFIX_AGENT}.handoff.to"] = to_agent

        return name, attributes, self._SpanKind.INTERNAL

    def _map_guardrail_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map guardrail span data to OTel format."""
        name = f"guardrail: {span_data.name}"
        attributes[f"{_ATTR_PREFIX_AGENT}.guardrail.name"] = span_data.name
        attributes[f"{_ATTR_PREFIX_AGENT}.guardrail.triggered"] = span_data.triggered

        return name, attributes, self._SpanKind.INTERNAL

    def _map_custom_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map custom span data to OTel format."""
        name = f"custom: {span_data.name}"
        attributes["custom.name"] = span_data.name

        if span_data.data:
            for key, value in span_data.data.items():
                attributes[f"custom.data.{key}"] = _safe_attribute_value(value)

        return name, attributes, self._SpanKind.INTERNAL

    def _map_response_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map response span data to OTel format."""
        name = "gen_ai.response"
        response = getattr(span_data, "response", None)

        if response and hasattr(response, "id"):
            attributes[f"{_ATTR_PREFIX_GEN_AI}.response.id"] = response.id

        return name, attributes, self._SpanKind.INTERNAL

    def _map_transcription_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map transcription (speech-to-text) span data to OTel format."""
        name = "audio.transcription"

        if span_data.model:
            attributes["audio.model"] = span_data.model
            name = f"audio.transcription: {span_data.model}"

        return name, attributes, self._SpanKind.CLIENT

    def _map_speech_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map speech (text-to-speech) span data to OTel format."""
        name = "audio.speech"

        if span_data.model:
            attributes["audio.model"] = span_data.model
            name = f"audio.speech: {span_data.model}"

        return name, attributes, self._SpanKind.CLIENT

    def _map_mcp_tools_span(
        self, span_data: Any, attributes: dict[str, Any]
    ) -> tuple[str, dict[str, Any], Any]:
        """Map MCP tools listing span data to OTel format."""
        name = "mcp.list_tools"
        server = getattr(span_data, "server", None)

        if server:
            attributes["mcp.server"] = server
            name = f"mcp.list_tools: {server}"

        return name, attributes, self._SpanKind.CLIENT

    def _update_span_with_final_data(self, otel_span: Any, span_data: Any) -> None:
        """Update an OTel span with final data available at span end.

        Args:
            otel_span: The OTel span to update.
            span_data: The SDK SpanData object with final values.
        """
        span_type = span_data.type

        if span_type == "generation":
            self._update_generation_span(otel_span, span_data)
        elif span_type == "function":
            self._update_function_span(otel_span, span_data)
        elif span_type == "transcription":
            output = getattr(span_data, "output", None)
            if output:
                otel_span.set_attribute(
                    "audio.transcription.output", _truncate_string(output, 2048)
                )
        elif span_type == "mcp_tools":
            result = getattr(span_data, "result", None)
            if result:
                otel_span.set_attribute("mcp.tools.count", len(result))
                otel_span.set_attribute("mcp.tools.list", json.dumps(result[:20]))

    def _update_generation_span(self, otel_span: Any, span_data: Any) -> None:
        """Update generation span with final usage metrics and output."""
        usage = getattr(span_data, "usage", None)
        if usage:
            if "input_tokens" in usage:
                otel_span.set_attribute(
                    f"{_ATTR_PREFIX_GEN_AI}.usage.input_tokens", usage["input_tokens"]
                )
            if "output_tokens" in usage:
                otel_span.set_attribute(
                    f"{_ATTR_PREFIX_GEN_AI}.usage.output_tokens", usage["output_tokens"]
                )
            if "total_tokens" in usage:
                otel_span.set_attribute(
                    f"{_ATTR_PREFIX_GEN_AI}.usage.total_tokens", usage["total_tokens"]
                )

        # Add truncated input/output preview for debugging
        input_data = getattr(span_data, "input", None)
        if input_data:
            try:
                input_preview = json.dumps(list(input_data)[:3])
                otel_span.set_attribute(
                    f"{_ATTR_PREFIX_GEN_AI}.input.preview", _truncate_string(input_preview, 1024)
                )
            except Exception:
                pass

        output_data = getattr(span_data, "output", None)
        if output_data:
            try:
                output_preview = json.dumps(list(output_data)[:3])
                otel_span.set_attribute(
                    f"{_ATTR_PREFIX_GEN_AI}.output.preview", _truncate_string(output_preview, 1024)
                )
            except Exception:
                pass

    def _update_function_span(self, otel_span: Any, span_data: Any) -> None:
        """Update function span with output."""
        output = getattr(span_data, "output", None)
        if output:
            otel_span.set_attribute("tool.output", _truncate_string(str(output), 4096))


def _safe_attribute_value(value: Any) -> str | int | float | bool:
    """Convert a value to a safe OTel attribute type.

    OpenTelemetry attributes must be primitive types (str, int, float, bool)
    or sequences thereof. This function converts complex types to JSON strings.

    Args:
        value: The value to convert.

    Returns:
        A primitive type suitable for OTel attributes.
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _truncate_string(value: str, max_length: int = 4096) -> str:
    """Truncate a string to a maximum length.

    Args:
        value: The string to truncate.
        max_length: Maximum length (default 4096).

    Returns:
        The truncated string with ellipsis if truncated.
    """
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."
