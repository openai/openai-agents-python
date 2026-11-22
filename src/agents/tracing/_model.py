"""Shared model operation lifecycle for provider adapters and Runner retries."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

from . import util
from .config import get_operation_detail
from .create import generation_span, response_span
from .provider import _is_noop_span
from .scope import Scope
from .span_data import GenerationSpanData, OperationMetadata, ResponseSpanData
from .spans import Span
from .traces import Trace

if TYPE_CHECKING:
    from ..models.interface import ModelTracing

ModelSpan = Span[GenerationSpanData] | Span[ResponseSpanData]
_Data = TypeVar("_Data", GenerationSpanData, ResponseSpanData)


def current_model_span() -> ModelSpan | None:
    span = Scope.get_current_span()
    if span is None or _is_noop_span(span):
        return None
    return span if isinstance(span.span_data, (GenerationSpanData, ResponseSpanData)) else None


def record_retries(count: int = 1, *, span: ModelSpan | None = None, at: str | None = None) -> None:
    """Record observed attempts only, without error text, headers or request payloads."""
    if type(count) is not int or count <= 0:
        return
    span = span or current_model_span()
    if span is not None:
        # Match the default OpenTelemetry event limit, even for unlimited retries.
        timestamps = span.span_data.operation.retry_timestamps
        timestamps.extend([at or util.time_iso()] * min(count, 128 - len(timestamps)))


def restart_model_span(
    span: ModelSpan, operation: OperationMetadata, *, at: str | None = None
) -> None:
    """Retain lifetime, retries and usage while replacing attempt-specific data."""
    data = span.span_data
    data.operation = OperationMetadata(
        detail=operation.detail,
        api_type=operation.api_type,
        model=operation.model,
        retry_timestamps=data.operation.retry_timestamps,
    )
    data.input = data.output = None
    if isinstance(data, ResponseSpanData):
        data.response = data._response_id = None
        data.model_config = None
    else:
        data.tools = None
    span.clear_error()
    record_retries(span=span, at=at)


@dataclass
class ModelRetrySpans:
    attempt: int = 0
    operations: int = 0
    active: ModelSpan | None = None
    pending: ModelSpan | None = None
    ended_at: str | None = None
    carried_usage: dict[str, Any] | None = None

    def begin_attempt(self, attempt: int) -> None:
        if attempt != self.attempt:
            self.attempt = attempt
            self.operations = 0
            self.active = None

    def finish(self) -> None:
        if self.pending is not None:
            span, self.pending = self.pending, None
            if isinstance(sys.exc_info()[1], asyncio.CancelledError):
                span.span_data.operation.outcome = "interrupted"
                span.span_data.operation.estimated_end = True
                self.ended_at = None
            if self.ended_at is not None:
                span.finish_at(self.ended_at)
            else:
                span.finish()


_retry_spans: ContextVar[ModelRetrySpans | None] = ContextVar("model_retry_spans", default=None)


@contextmanager
def model_retry_scope(group: ModelRetrySpans, attempt: int) -> Iterator[None]:
    group.begin_attempt(attempt)
    token = _retry_spans.set(group)
    try:
        yield
    finally:
        _retry_spans.reset(token)


def acquire_model_span(
    tracing: ModelTracing,
    model: str | None = None,
    *,
    parent: Trace | Span[Any] | None = None,
    started_at: str | None = None,
) -> Span[ResponseSpanData]:
    span = response_span(parent=parent, disabled=tracing.is_disabled())
    span.span_data.operation.api_type = "responses"
    span.span_data.operation.detail = get_operation_detail(include_data=tracing.include_data())
    span.span_data.operation.model = model
    return _start(span, started_at)


def _start(span: Span[_Data], started_at: str | None = None) -> Span[_Data]:
    group = _retry_spans.get()
    if group is not None:
        group.operations += 1
        pending = group.pending
        if (
            group.operations == 1
            and pending is not None
            and pending.trace_id == span.trace_id
            and type(pending.span_data) is type(span.span_data)
        ):
            group.pending = None
            group.ended_at = None
            group.carried_usage = pending.span_data.usage
            pending.span_data.usage = None
            # A retry policy may have revoked capture permission since the first attempt.
            restart_model_span(pending, span.span_data.operation, at=started_at)
            pending.span_data.model_config = span.span_data.model_config
            group.active = pending
            return cast("Span[_Data]", pending)
        group.finish()
        group.active = span
    if started_at is not None:
        span.start_at(started_at)
    else:
        span.start()
    return span


def finish_model_span(span: ModelSpan, *, ended_at: str | None = None) -> None:
    operation = span.span_data.operation
    group = _retry_spans.get()
    if span.ended_at is not None or (group is not None and group.pending is span):
        return
    if operation.outcome is None:
        operation.outcome = "failed" if span.error is not None else "completed"
    if group is not None and group.active is not span:
        group = None
    if group is not None and group.carried_usage is not None:
        span.span_data.usage = _add_usage(group.carried_usage, span.span_data.usage or {})
        group.carried_usage = None
    if (
        group is not None
        and group.operations == 1
        and operation.outcome in ("failed", "interrupted")
    ):
        group.finish()
        group.pending = span
        group.ended_at = ended_at or util.time_iso()
        return
    if ended_at is not None:
        span.finish_at(ended_at)
    else:
        span.finish()


def model_span(
    tracing: ModelTracing, model: str | None = None
) -> AbstractContextManager[Span[ResponseSpanData]]:
    return _model_scope(acquire_model_span(tracing, model))


def chat_model_span(
    *, model: str, model_config: Mapping[str, Any], tracing: ModelTracing
) -> AbstractContextManager[Span[GenerationSpanData]]:
    span = generation_span(model=model, disabled=tracing.is_disabled())
    span.span_data.operation.detail = get_operation_detail(include_data=tracing.include_data())
    span.span_data.model_config = model_config
    return _model_scope(_start(span))


@contextmanager
def _model_scope(span: Span[_Data]) -> Iterator[Span[_Data]]:
    with span.as_current():
        try:
            yield span
        except BaseException as error:
            if isinstance(error, Exception) or span.error is not None:
                span.span_data.operation.outcome = "failed"
            elif span.span_data.operation.outcome is None:
                span.span_data.operation.outcome = "interrupted"
            raise
        finally:
            finish_model_span(span)


def _add_usage(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = dict(previous)
    for key, value in current.items():
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _add_usage(old, value)
        elif isinstance(old, int) and isinstance(value, int):
            result[key] = old + value
        else:
            result[key] = value
    return result


def record_model_input(data: ResponseSpanData, request: Mapping[str, Any]) -> None:
    """Capture the approved request fields under one shared input-direction budget."""
    from ._content import bound_content

    if data.operation.detail != "content":
        return
    previous_response_id = request.get("previous_response_id")
    if isinstance(previous_response_id, str):
        data.operation.previous_response_id = previous_response_id
    inputs = request.get("input")
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    approved = {"input": inputs}
    instructions = request.get("instructions")
    if isinstance(instructions, str):
        approved["instructions"] = instructions
    captured = bound_content(approved)
    if not isinstance(captured, dict):
        captured = {"input": [{"omitted": True, "reason": "serialization_failed"}]}
    if captured.get("truncated"):
        items = captured.get("input") or []
        captured["input"] = [*items, {"truncated": True}]
    data.input = captured.get("input")
    data.model_config = (
        {"instructions": captured["instructions"]} if "instructions" in captured else None
    )
