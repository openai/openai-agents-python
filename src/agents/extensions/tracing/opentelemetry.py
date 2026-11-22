"""Translate native SDK spans into live OpenTelemetry spans.

Install ``openai-agents[opentelemetry]`` and register this processor with
``agents.add_trace_processor``. Configure exporters and sampling on the application's
OpenTelemetry tracer provider; this processor does not own that provider's lifecycle.

The GenAI mapping follows semantic-conventions-genai commit
``622b2b6073b15e5ed886c3c12a65a892b9cd5c70``. These conventions are in development.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any

from opentelemetry import context as otel_context, trace as otel_trace
from opentelemetry.trace import SpanKind, StatusCode, TracerProvider

from ...logger import logger
from ...tracing.processor_interface import TracingProcessor
from ...tracing.span_data import (
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    ResponseSpanData,
    SpanData,
    TaskSpanData,
    TurnSpanData,
)
from ...tracing.spans import Span
from ...tracing.traces import Trace
from ...tracing.util import _parse_iso_timestamp
from ...version import __version__

_scope_owner: ContextVar[object | None] = ContextVar("agents_otel_scope_owner", default=None)


def _timestamp(value: str | None) -> int | None:
    if value is None:
        return None
    parsed, nanos = _parse_iso_timestamp(value)
    seconds = int(parsed.timestamp())
    return seconds * 1_000_000_000 + nanos


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _messages(value: Any, *, output: bool = False) -> list[dict[str, Any]]:
    """Project Responses text and tool content into the GenAI message schema."""
    if isinstance(value, str):
        return [
            {
                "role": "assistant" if output else "user",
                "parts": [{"type": "text", "content": value}],
            }
        ]
    if not isinstance(value, Sequence):
        return []
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type", "message")
        role = item.get("role", "assistant" if output else "user")
        parts: list[dict[str, Any]] = []
        if kind in ("input_text", "output_text", "text"):
            part = {"type": "text", "content": item.get("text", "")}
            for flag in ("partial", "truncated"):
                if isinstance(item.get(flag), bool):
                    part[flag] = item[flag]
            parts.append(part)
        elif (
            (item.get("truncated") is True or item.get("omitted") is True)
            and "content" not in item
            and "tool_calls" not in item
        ):
            reason = item.get("reason")
            parts.append(
                {
                    "type": "openai.omitted",
                    "reason": reason if isinstance(reason, str) else "truncated",
                }
            )
        elif kind in ("function_call", "custom_tool_call"):
            role = "assistant"
            parts.append(
                {
                    "type": "tool_call",
                    "name": item.get("name", ""),
                    "id": item.get("call_id"),
                    "arguments": _json_value(item.get("arguments", item.get("input"))),
                }
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            role = "tool"
            parts.append(
                {
                    "type": "tool_call_response",
                    "id": item.get("call_id"),
                    "response": item.get("output"),
                }
            )
        elif kind == "message":
            content = item.get("content")
            if role == "tool" and "tool_call_id" in item:
                parts.append(
                    {"type": "tool_call_response", "id": item["tool_call_id"], "response": content}
                )
            elif isinstance(content, str):
                parts.append({"type": "text", "content": content})
            elif isinstance(content, Sequence):
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    if part.get("type") in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "content": part.get("text", "")})
                    elif part.get("type") == "refusal":
                        parts.append({"type": "text", "content": part.get("refusal", "")})
                    else:
                        # Preserve part order without exporting media or hidden reasoning.
                        parts.append({"type": "openai.omitted", "reason": "unsupported_content"})
            refusal = item.get("refusal")
            if isinstance(refusal, str):
                parts.append({"type": "text", "content": refusal})
            for call in item.get("tool_calls") or ():
                if not isinstance(call, Mapping) or call.get("type") != "function":
                    continue
                function = call.get("function")
                if isinstance(function, Mapping):
                    canonical = {
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments"),
                    }
                    parts.extend(_messages([canonical], output=True)[0]["parts"])
        else:
            parts.append({"type": "openai.omitted", "reason": "unsupported_content"})
        if output and messages:
            messages[0]["parts"].extend(parts)
        else:
            messages.append({"role": role, "parts": parts})
    return messages


def _message_schema(value: Any, role: str) -> list[dict[str, Any]]:
    """Preserve the message schema when the shared budget replaces a container."""
    messages: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else [{}]:
        if not isinstance(item, Mapping) or "parts" not in item:
            messages.append(
                {"role": role, "parts": [{"type": "openai.omitted", "reason": "truncated"}]}
            )
            continue
        parts = [
            part
            if isinstance(part, Mapping) and "type" in part
            else {"type": "openai.omitted", "reason": "truncated"}
            for part in item["parts"]
        ]
        messages.append({"role": item.get("role", role), "parts": parts})
    return messages


def _identity(data: SpanData) -> tuple[str, str, SpanKind]:
    if isinstance(data, TaskSpanData):
        return f"invoke_runner {data.name}", "invoke_runner", SpanKind.INTERNAL
    if isinstance(data, AgentSpanData):
        return f"invoke_agent {data.name}", "invoke_agent", SpanKind.INTERNAL
    if isinstance(data, TurnSpanData):
        return "openai.agents.turn", "openai.agents.turn", SpanKind.INTERNAL
    if isinstance(data, FunctionSpanData):
        if data.operation.outcome in ("paused", "rejected"):
            return (
                f"openai.agents.tool_approval {data.name}",
                "openai.agents.tool_approval",
                SpanKind.INTERNAL,
            )
        return f"execute_tool {data.name}", "execute_tool", SpanKind.INTERNAL
    if isinstance(data, (GenerationSpanData, ResponseSpanData)):
        model = data.operation.model or getattr(data, "model", None)
        if isinstance(data, ResponseSpanData) and data.response is not None:
            model = model or data.response.model
        return f"chat {model}" if model else "chat", "chat", SpanKind.CLIENT
    if isinstance(data, HandoffSpanData):
        return "openai.agents.handoff", "openai.agents.handoff", SpanKind.INTERNAL
    if isinstance(data, GuardrailSpanData):
        return f"openai.agents.guardrail {data.name}", "openai.agents.guardrail", SpanKind.INTERNAL
    if isinstance(data, CustomSpanData):
        if data.name == "tool_approval":
            name = data.data.get("tool_name")
            return (
                f"openai.agents.tool_approval {name}" if name else "openai.agents.tool_approval",
                "openai.agents.tool_approval",
                SpanKind.INTERNAL,
            )
        return data.name, "openai.agents.custom", SpanKind.INTERNAL
    return f"openai.agents.{data.type}", f"openai.agents.{data.type}", SpanKind.INTERNAL


class OpenTelemetryTracingProcessor(TracingProcessor):
    """Export native SDK spans through the application's OpenTelemetry provider.

    Content permission belongs to each SDK operation. Legacy SDK payload fields do not
    grant permission to export content through this integration. ``force_flush`` and
    ``shutdown`` do not flush or shut down the caller-owned OpenTelemetry provider.
    """

    def __init__(self, *, tracer_provider: TracerProvider | None = None) -> None:
        self._tracer = otel_trace.get_tracer(
            "openai.agents", __version__, tracer_provider=tracer_provider
        )
        self._lock = threading.RLock()
        self._spans: dict[tuple[str, str], otel_trace.Span] = {}
        self._parents: dict[tuple[str, str], otel_trace.Span] = {}
        self._traces: dict[str, dict[int, Trace]] = {}

    def on_trace_start(self, trace: Trace) -> None:
        # A native trace may group several Runner invocations; task spans represent runs.
        with self._lock:
            self._traces.setdefault(trace.trace_id, {})[id(trace)] = trace

    def on_trace_end(self, trace: Trace) -> None:
        with self._lock:
            owners = self._traces.get(trace.trace_id)
            if owners is None or id(trace) not in owners:
                return
            owners.pop(id(trace))
            if owners:
                return
            self._traces.pop(trace.trace_id, None)
            unfinished = [
                self._spans.pop(key) for key in list(self._spans) if key[0] == trace.trace_id
            ]
            for key in [key for key in self._parents if key[0] == trace.trace_id]:
                self._parents.pop(key, None)
        self._end_interrupted(unfinished)

    def on_span_start(self, span: Span[Any]) -> None:
        key = (span.trace_id, span.span_id)
        with self._lock:
            if key in self._spans:
                return
            parent = self._parents.get((span.trace_id, span.parent_id or ""))
            context = otel_trace.set_span_in_context(parent) if parent is not None else None
            name, operation, kind = _identity(span.span_data)
            attributes = self._attributes(span)
            attributes["gen_ai.operation.name"] = operation
            attributes["openai.agents.span.type"] = span.span_data.type
            otel_span = self._tracer.start_span(
                name,
                context=context,
                kind=kind,
                attributes=attributes,
                start_time=_timestamp(span.started_at),
            )
            self._spans[key] = otel_span
            # Keep only identity after completion, not exported content or SDK resources.
            self._parents[key] = otel_trace.NonRecordingSpan(otel_span.get_span_context())

    def on_span_end(self, span: Span[Any]) -> None:
        with self._lock:
            otel_span = self._spans.pop((span.trace_id, span.span_id), None)
            if span.trace_id not in self._traces and not any(
                key[0] == span.trace_id for key in self._spans
            ):
                for key in [key for key in self._parents if key[0] == span.trace_id]:
                    self._parents.pop(key, None)
        if otel_span is None:
            return
        try:
            otel_span.set_attributes(self._attributes(span))
            if span.error is not None:
                # Error messages may contain payloads even when content capture is disabled.
                otel_span.set_attribute("error.type", "_OTHER")
                otel_span.set_status(StatusCode.ERROR)
            operation = getattr(span.span_data, "operation", None)
            if operation is not None and operation.outcome in (
                "error",
                "failed",
                "cancelled",
                "interrupted",
            ):
                otel_span.set_attribute("error.type", "_OTHER")
                otel_span.set_status(StatusCode.ERROR)
        finally:
            otel_span.end(end_time=_timestamp(span.ended_at))

    def on_span_scope_enter(self, span: Span[Any]) -> object | None:
        """Activate a span only for the SDK execution scope that owns its context."""
        with self._lock:
            otel_span = self._spans.get((span.trace_id, span.span_id))
        if otel_span is None:
            return None
        owner_token = _scope_owner.set(object())
        return owner_token, otel_context.attach(otel_trace.set_span_in_context(otel_span))

    def on_span_scope_exit(self, span: Span[Any], token: Any) -> None:
        if token is not None:
            owner_token, context_token = token
            try:
                _scope_owner.reset(owner_token)
            except ValueError:
                # Async-generator finalization cannot reset the originating task's context.
                return
            otel_context.detach(context_token)

    def _attributes(self, span: Span[Any]) -> dict[str, Any]:
        data = span.span_data
        attributes: dict[str, Any] = {}
        if isinstance(data, (GenerationSpanData, ResponseSpanData)):
            if isinstance(data, ResponseSpanData) or data.operation.api_type == "responses":
                attributes["gen_ai.provider.name"] = "openai"
                attributes["openai.api.type"] = "responses"
            model = data.operation.model or getattr(data, "model", None)
            if isinstance(data, ResponseSpanData) and data.response is not None:
                model = model or data.response.model
            if model:
                attributes["gen_ai.request.model"] = model
            usage = data.usage
            if usage:
                for source, destination in (
                    ("input_tokens", "gen_ai.usage.input_tokens"),
                    ("output_tokens", "gen_ai.usage.output_tokens"),
                ):
                    if isinstance(usage.get(source), int):
                        attributes[destination] = usage[source]
                for group, source, destination in (
                    (
                        "input_tokens_details",
                        "cached_tokens",
                        "gen_ai.usage.cache_read.input_tokens",
                    ),
                    (
                        "input_tokens_details",
                        "cache_write_tokens",
                        "gen_ai.usage.cache_write.input_tokens",
                    ),
                    (
                        "output_tokens_details",
                        "reasoning_tokens",
                        "gen_ai.usage.reasoning.output_tokens",
                    ),
                ):
                    details = usage.get(group)
                    value = details.get(source) if isinstance(details, Mapping) else None
                    if isinstance(value, int) and value != 0:
                        attributes[destination] = value
            if data.operation.response_model:
                attributes["gen_ai.response.model"] = data.operation.response_model
            if data.operation.response_id:
                attributes["gen_ai.response.id"] = data.operation.response_id
            if data.operation.previous_response_id:
                attributes["openai.response.previous_response_id"] = (
                    data.operation.previous_response_id
                )
        if isinstance(data, (AgentSpanData, TurnSpanData)):
            attributes["gen_ai.agent.name"] = (
                data.name if isinstance(data, AgentSpanData) else data.agent_name
            )
        if isinstance(data, TurnSpanData):
            attributes["openai.agents.turn.number"] = data.turn
        if isinstance(data, FunctionSpanData):
            attributes["gen_ai.tool.name"] = data.name
            attributes["gen_ai.tool.type"] = "function"
        if isinstance(data, (GenerationSpanData, ResponseSpanData, FunctionSpanData)):
            operation = data.operation
            if operation.outcome:
                attributes["openai.agents.operation.outcome"] = operation.outcome
            if operation.estimated_end:
                attributes["openai.agents.operation.estimated_end"] = True
            if operation.operation_id:
                attributes["openai.agents.operation.id"] = operation.operation_id
            if operation.tool_call_id:
                attributes["gen_ai.tool.call.id"] = operation.tool_call_id
            if operation.detail == "content":
                attributes.update(self._content_attributes(data))
        if isinstance(data, CustomSpanData) and data.name == "codex.tracing.coverage":
            for key in ("provider", "coverage", "reason"):
                if isinstance(data.data.get(key), str):
                    attributes[f"openai.agents.operation.{key}"] = data.data[key]
        if isinstance(data, CustomSpanData) and data.name == "tool_approval":
            for source, target in (
                ("tool_name", "gen_ai.tool.name"),
                ("tool_call_id", "gen_ai.tool.call.id"),
                ("outcome", "openai.agents.operation.outcome"),
            ):
                if isinstance(data.data.get(source), str):
                    attributes[target] = data.data[source]
        return attributes

    def _content_attributes(
        self, data: GenerationSpanData | ResponseSpanData | FunctionSpanData
    ) -> dict[str, Any]:
        from ...tracing._content import bound_content

        values: dict[str, Any] = {}
        if isinstance(data, FunctionSpanData):
            if data.input is not None:
                values["gen_ai.tool.call.arguments"] = bound_content(_json_value(data.input))
            if data.output is not None:
                values["gen_ai.tool.call.result"] = bound_content(data.output)
        else:
            if isinstance(data, ResponseSpanData):
                inputs = bound_content(data.input)
                output = bound_content(data.response.output) if data.response is not None else None
            else:
                inputs = data.input
                output = data.output
            request: dict[str, Any] = {}
            if inputs is not None:
                request["messages"] = _messages(inputs)
            if output is not None:
                values["gen_ai.output.messages"] = _message_schema(
                    bound_content(_messages(output, output=True)), "assistant"
                )
            if isinstance(data, GenerationSpanData) and data.model_config:
                instructions = data.model_config.get("instructions")
                if isinstance(instructions, str):
                    request["instructions"] = [{"type": "text", "content": instructions}]
            if isinstance(data, GenerationSpanData) and data.tools is not None:
                request["tools"] = data.tools
            request = bound_content(request)
            if "messages" in request:
                values["gen_ai.input.messages"] = _message_schema(request["messages"], "user")
            if "instructions" in request:
                values["gen_ai.system_instructions"] = request["instructions"]
            if "tools" in request:
                values["gen_ai.tool.definitions"] = request["tools"]
        return values

    def force_flush(self) -> None:
        """Leave exporter flushing to the application's OpenTelemetry provider."""
        pass

    @staticmethod
    def _end_interrupted(spans: list[otel_trace.Span]) -> None:
        for span in spans:
            try:
                try:
                    span.set_attributes(
                        {
                            "openai.agents.operation.outcome": "interrupted",
                            "openai.agents.operation.estimated_end": True,
                            "error.type": "_OTHER",
                        }
                    )
                    span.set_status(StatusCode.ERROR)
                finally:
                    span.end()
            except Exception:
                logger.warning("Error while ending an interrupted OpenTelemetry span")

    def shutdown(self) -> None:
        """End outstanding spans without shutting down the application's provider."""
        with self._lock:
            spans = list(self._spans.values())
            self._spans.clear()
            self._parents.clear()
            self._traces.clear()
        self._end_interrupted(spans)
