from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai.types.responses import Response

from ..tracing import (
    Span,
    custom_span,
    function_span,
    get_current_span,
    get_current_trace,
    response_span,
)
from ..tracing._content import _json, bound_content
from ..tracing._model import acquire_model_span, finish_model_span, record_model_input
from ..tracing.config import get_operation_detail
from ..tracing.provider import _is_noop_span, _is_noop_trace
from ..tracing.span_data import FunctionSpanData, GenerationSpanData, ResponseSpanData
from ..tracing.util import _parse_iso_timestamp, time_iso
from .interface import ModelTracing


@dataclass
class _Operation:
    span: Span[Any]
    kind: str
    ended_at: str | None = None


class CodexOperationTracing:
    """Translate turn-private operation facts into the current SDK trace."""

    def __init__(self, tracing: ModelTracing, input: Any, model: str | None) -> None:
        self._tracing = tracing
        self._detail = get_operation_detail(include_data=tracing.include_data())
        self._input = input
        self._model = model
        self._parent = get_current_span()
        active_trace = get_current_trace()
        disabled = (
            tracing.is_disabled()
            or active_trace is None
            or _is_noop_trace(active_trace)
            or _is_noop_span(self._parent)
        )
        self.mode = None if disabled else self._detail or "metadata"
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._operations: dict[str, _Operation] = {}
        self._completed: dict[str, str] = {}
        self._pending: _Operation | None = None
        self._first_model = True
        self._coverage_reason: str | None = None
        self._coverage_emitted = False

    def request_mode(self) -> str | None:
        # A hook may disable the provider after the active trace and parent were created.
        if self.mode is not None and _is_noop_span(response_span(parent=self._parent)):
            self.mode = None
        return self.mode

    def bind(self, thread_id: str, turn_id: str) -> None:
        self._thread_id, self._turn_id = thread_id, turn_id

    def observe(self, method: str, event: Any) -> None:
        if self.mode is None:
            return
        try:
            if method == "turn/operation/dropped":
                if event.get("threadId") != self._thread_id:
                    raise ValueError("Unexpected observation thread.")
                self._coverage_reason = "dropped_operations"
                return
            self._observe(method, event)
        except Exception:
            # Telemetry must never replay a model request or interrupt tool execution.
            self._coverage_reason = "telemetry_error"

    def _observe(self, method: str, event: dict[str, Any]) -> None:
        if event.get("threadId") != self._thread_id or event.get("turnId") != self._turn_id:
            self._coverage_reason = "telemetry_error"
            return
        operation_id = event["operationId"]
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("Missing operation identity.")
        if method == "turn/operation/started":
            if operation_id in self._operations or operation_id in self._completed:
                raise ValueError("Duplicate operation start.")
            kind = event["kind"]
            started_at = self._timestamp(event["startedAt"])
            parent_id = event.get("parentOperationId")
            parent = self._parent
            if parent_id is not None:
                if parent_id in self._operations:
                    parent = self._operations[parent_id].span
                else:
                    # Only identity is needed when a later child refers to a completed parent.
                    parent = function_span(
                        "codex.operation.parent",
                        span_id=self._completed[parent_id],
                        parent=self._parent,
                    )
            span: Span[Any]
            if kind == "model":
                self._finish_pending()
                span = acquire_model_span(
                    self._tracing,
                    event.get("requestedModel") or self._model,
                    parent=parent,
                    started_at=started_at,
                )
                if isinstance(span.span_data, ResponseSpanData) and self._first_model:
                    if self._tracing.include_data():
                        span.span_data.input = self._input
                self._first_model = False
            elif kind == "tool":
                span = function_span(event["toolName"], parent=parent)
                span.span_data.operation.detail = self._detail
            else:
                raise ValueError("Unknown operation kind.")
            operation = _Operation(span=span, kind=kind)
            self._operations[operation_id] = operation
            self._metadata(operation, event)
            self._content(operation, event)
            if kind == "tool":
                span.start_at(started_at)
            return
        operation = self._operations[operation_id]
        if operation.ended_at is not None or event.get("kind") != operation.kind:
            raise ValueError("Invalid operation terminal.")
        ended_at = self._timestamp(event["completedAt"])
        outcome = event["outcome"]
        if outcome not in ("success", "error", "interrupted"):
            raise ValueError("Unknown operation outcome.")
        self._metadata(operation, event)
        self._content(operation, event)
        operation.ended_at = ended_at
        operation.span.span_data.operation.outcome = {
            "success": "completed",
            "error": "failed",
        }.get(outcome, outcome)
        if outcome != "success":
            operation.span.set_error({"message": f"Codex operation {outcome}.", "data": None})
        if operation.kind == "model":
            self._finish_pending()
            self._pending = operation
        else:
            operation.span.finish_at(operation.ended_at)
        self._completed[operation_id] = operation.span.span_id
        self._operations.pop(operation_id)

    @staticmethod
    def _timestamp(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Missing source timestamp.")
        _parse_iso_timestamp(value)
        return value

    def _metadata(self, operation: _Operation, event: dict[str, Any]) -> None:
        data = operation.span.span_data
        metadata = data.operation
        metadata.operation_id = event["operationId"]
        metadata.model = event.get("requestedModel") or metadata.model or self._model
        metadata.response_model = event.get("observedModel") or metadata.response_model
        metadata.tool_call_id = event.get("callId") or metadata.tool_call_id
        if self._tracing.include_data() or event.get("responseIdTrusted") is True:
            metadata.response_id = event.get("responseId") or metadata.response_id
        else:
            metadata.response_id = None
        usage = event.get("usage")
        if operation.kind == "model":
            if isinstance(data, GenerationSpanData):
                data.model = metadata.model
            if isinstance(usage, dict):
                data.usage = {key: value for key, value in usage.items() if isinstance(value, int)}

    def _content(self, operation: _Operation, event: dict[str, Any]) -> None:
        if self._detail != "content":
            return
        data = operation.span.span_data
        for direction in ("input", "output"):
            projection = event.get(direction)
            if not isinstance(projection, dict):
                continue
            value = projection.get("value")
            marker: dict[str, Any] = {
                key: True for key in ("truncated", "omitted") if projection.get(key) is True
            }
            if marker and isinstance(projection.get("reason"), str):
                marker["reason"] = projection["reason"]
            if isinstance(data, GenerationSpanData) and direction == "input":
                request = dict(value) if isinstance(value, dict) else {"input": value}
                request.update(marker)
                record_model_input(data, request)
                continue
            value = bound_content(value)
            if value is None and marker:
                value = marker
            if isinstance(data, FunctionSpanData):
                setattr(data, direction, value if isinstance(value, str) else _json(value))
            elif isinstance(data, GenerationSpanData):
                items = value if isinstance(value, list) else [{"content": value}]
                if marker and marker not in items:
                    items = [*items, marker]
                data.output = bound_content(items)

    def _finish_pending(self) -> None:
        if self._pending is not None:
            pending, self._pending = self._pending, None
            finish_model_span(pending.span, ended_at=pending.ended_at)

    def close(self, *, interrupted: bool) -> None:
        if self.mode is None:
            return
        estimated_end = time_iso()
        for operation_id, operation in self._operations.items():
            if operation.ended_at is not None:
                continue
            operation.ended_at = estimated_end
            operation.span.span_data.operation.outcome = "interrupted"
            operation.span.span_data.operation.estimated_end = True
            operation.span.set_error({"message": "Codex operation interrupted.", "data": None})
            if operation.kind == "model":
                finish_model_span(operation.span, ended_at=operation.ended_at)
            else:
                operation.span.finish_at(operation.ended_at)
            self._completed[operation_id] = operation.span.span_id
            self._coverage_reason = "interrupted"
        self._operations.clear()
        if interrupted:
            if not self._completed:
                self._coverage_reason = self._coverage_reason or "interrupted"
            self._finish_pending()
        self._emit_coverage()

    def finish_response(self, response: Response) -> None:
        if self.mode is not None and self._first_model:
            self._coverage_reason = "missing_operations"
        if self._pending is not None:
            data = self._pending.span.span_data
            if isinstance(data, ResponseSpanData) and self._tracing.include_data():
                data.response = response
            self._finish_pending()
        self.close(interrupted=False)

    def _emit_coverage(self) -> None:
        if self._coverage_reason is None or self._coverage_emitted:
            return
        self._coverage_emitted = True
        with custom_span(
            "codex.tracing.coverage",
            {"coverage": "partial", "provider": "codex", "reason": self._coverage_reason},
            parent=self._parent,
        ):
            pass
