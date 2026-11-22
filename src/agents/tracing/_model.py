"""Shared model operation lifecycle for provider adapters and Runner retries."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import util
from .config import get_operation_detail
from .create import generation_span, response_span
from .span_data import GenerationSpanData, OperationMetadata, ResponseSpanData
from .spans import Span
from .traces import Trace

if TYPE_CHECKING:
    from ..models.interface import ModelTracing

ModelSpan = Span[GenerationSpanData] | Span[ResponseSpanData]


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
) -> ModelSpan:
    detail = get_operation_detail(include_data=tracing.include_data())
    group = _retry_spans.get() if detail is not None else None
    if group is not None:
        if group.operations == 0 and group.pending is not None:
            span, group.pending = group.pending, None
            group.ended_at = None
            group.carried_usage = span.span_data.usage
            span.span_data.usage = None
            span.span_data.operation = OperationMetadata(
                detail=detail, api_type="responses", model=model
            )
            if isinstance(span.span_data, GenerationSpanData):
                span.span_data.input = None
                span.span_data.output = None
                span.span_data.model_config = None
                span.span_data.tools = None
                span.span_data.model = model
            span.clear_error()
            group.operations += 1
            group.active = span
            return span
        group.finish()
        group.operations += 1
    span = (
        generation_span(model=model, parent=parent, disabled=tracing.is_disabled())
        if detail is not None
        else response_span(parent=parent, disabled=tracing.is_disabled())
    )
    if group is not None:
        group.active = span
    span.span_data.operation.api_type = "responses"
    span.span_data.operation.detail = detail
    span.span_data.operation.model = model
    if started_at is not None:
        span.start_at(started_at)
    else:
        span.start()
    return span


def finish_model_span(span: ModelSpan, *, ended_at: str | None = None) -> None:
    operation = span.span_data.operation
    group = _retry_spans.get() if operation.detail is not None else None
    if span.ended_at is not None or (group is not None and group.pending is span):
        return
    if operation.outcome is None:
        operation.outcome = "failed" if span.error is not None else "completed"
    if group is not None and group.active is not span:
        group = None
    if group is not None and group.carried_usage is not None:
        span.span_data.usage = _add_usage(group.carried_usage, span.span_data.usage or {})
        group.carried_usage = None
    if group is not None and group.operations == 1 and operation.outcome == "failed":
        group.finish()
        group.pending = span
        group.ended_at = ended_at or util.time_iso()
        return
    if ended_at is not None:
        span.finish_at(ended_at)
    else:
        span.finish()


@contextmanager
def model_span(tracing: ModelTracing, model: str | None = None) -> Iterator[ModelSpan]:
    span = acquire_model_span(tracing, model)
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


def record_model_input(data: GenerationSpanData, request: Any) -> None:
    """Capture the approved request fields under one shared input-direction budget."""
    from ._content import bound_content

    if data.operation.detail != "content":
        return
    if not isinstance(request, dict):
        data.input = [{"omitted": True, "reason": "unsupported input projection"}]
        return
    previous_response_id = request.get("previous_response_id")
    if isinstance(previous_response_id, str):
        data.operation.previous_response_id = previous_response_id
    approved = {
        key: request[key] for key in ("input", "truncated", "omitted", "reason") if key in request
    }
    if isinstance(approved.get("input"), str):
        approved["input"] = [{"role": "user", "content": approved["input"]}]
    instructions = request.get("instructions")
    if isinstance(instructions, str):
        approved["instructions"] = instructions
    captured = bound_content(approved)
    if not isinstance(captured, dict):
        data.input = [{"omitted": True, "reason": "input serialization failed"}]
        return
    if "input" not in captured and captured.get("omitted"):
        data.input = [captured]
        return
    marker = {key: captured[key] for key in ("truncated", "omitted", "reason") if key in captured}
    if marker:
        items = captured.get("input", [])
        if isinstance(items, list):
            # Rebudget after moving the outer marker so exporters cannot lose it on extraction.
            with_marker = {"input": [*items, marker]}
            if "instructions" in captured:
                with_marker["instructions"] = captured["instructions"]
            captured = bound_content(with_marker)
    items = captured.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    data.input = items
    data.model_config = (
        {"instructions": captured["instructions"]} if "instructions" in captured else None
    )
