"""Translate bounded native observations without affecting model or tool execution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
from ..tracing.provider import _is_noop_span
from ..tracing.span_data import FunctionSpanData, GenerationSpanData, ResponseSpanData
from ..usage import _response_usage_to_usage, model_usage_to_span_usage
from ._codex_usage import _usage
from .interface import ModelTracing


class CodexOperationTracing:
    """Own observations for one ephemeral native thread, including tool restoration."""

    def __init__(
        self, sdk_tools: set[tuple[str | None, str]], *, expect_model: bool = True
    ) -> None:
        self._sdk_tools = sdk_tools
        self._expect_model = expect_model
        self._parent = get_current_span() or get_current_trace()
        self._thread: Any = None
        self._active: dict[tuple[str, str], Span[Any]] = {}
        self._ends: dict[tuple[str, str], tuple[str, str]] = {}
        self._ignored: set[str] = set()
        self._reason: str | None = None
        self._saw_model = False
        self._tracing = ModelTracing.ENABLED_WITHOUT_DATA
        self._capture = False

    def bind(self, thread: Any, tracing: ModelTracing) -> None:
        if tracing.is_disabled() or self._parent is None:
            return
        if _is_noop_span(response_span(parent=self._parent)):
            return
        self._capture = (
            tracing.include_data() and get_operation_detail(include_data=True) == "content"
        )
        self._tracing = tracing if self._capture else ModelTracing.ENABLED_WITHOUT_DATA
        try:
            if self._capture:
                thread.enable_tracing("content")
            else:
                thread.enable_tracing()
            self._thread = thread
        except Exception:
            self._reason = "unavailable"

    async def watch(self) -> None:
        if self._thread is None:
            return
        try:
            while True:
                await self._thread.wait_for_operations()
                self.drain()
        except Exception:
            self._reason = "telemetry_error"

    def drain(self) -> None:
        if self._thread is None:
            return
        try:
            encoded = self._thread.drain_operations()
            if not encoded or encoded == "[]":
                return
            for event in json.loads(encoded):
                self._observe(event)
        except Exception:
            self._reason = "telemetry_error"

    def _observe(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        if kind == "dropped":
            self._reason = "dropped_operations"
            return
        phase = event["phase"]
        if kind == "tool":
            if phase == "start" and (event.get("namespace"), event["name"]) in self._sdk_tools:
                self._ignored.add(event["id"])
            if event["id"] in self._ignored:
                return
        key = kind, event["id"]
        if phase in ("input", "output"):
            span = self._active.get(key)
            if span is not None:
                self._content(span, kind, event)
                if phase == "output" and key in self._ends:
                    outcome, at = self._ends.pop(key)
                    self._finish(key, outcome, at)
            return
        at = datetime.fromtimestamp(event["at"] / 1000, tz=timezone.utc).isoformat()
        if phase == "start":
            if key in self._active or len(self._active) >= 128:
                self._reason = "dropped_operations"
                return
            if kind == "model":
                span = acquire_model_span(
                    self._tracing, event["model"], parent=self._parent, started_at=at
                )
                self._saw_model = True
            elif kind == "tool":
                name = event["name"]
                if event.get("namespace"):
                    name = f"{event['namespace']}.{name}"
                span = function_span(name, parent=self._parent)
                span.span_data.operation.detail = get_operation_detail(include_data=self._capture)
                span.span_data.operation.tool_call_id = event["id"]
                span.start_at(at)
            else:
                raise ValueError("Unknown native operation.")
            self._active[key] = span
            return
        if key not in self._active:
            # Rejected calls never reached the post-authorization execution boundary.
            return
        span = self._active[key]
        if kind == "model":
            if event.get("usage") is not None:
                span.span_data.usage = model_usage_to_span_usage(
                    _response_usage_to_usage(_usage(event["usage"]))
                )
            self._content(span, kind, event)
        if kind == "tool" and self._capture:
            # Core publishes the accepted model-visible output after the lifecycle callback.
            self._ends[key] = event["outcome"], at
        else:
            self._finish(key, event["outcome"], at)

    def _content(self, span: Span[Any], kind: str, event: dict[str, Any]) -> None:
        if not self._capture:
            return
        data = span.span_data
        if kind == "tool" and isinstance(data, FunctionSpanData):
            for direction in ("input", "output"):
                if direction in event:
                    value = bound_content(event[direction])
                    setattr(data, direction, value if isinstance(value, str) else _json(value))
            return
        if "input" in event:
            request = bound_content(event["input"])
            if isinstance(data, GenerationSpanData):
                record_model_input(data, request)
                if isinstance(request, dict):
                    data.tools = bound_content(request.get("tools"))
                    config = {
                        key: request[key]
                        for key in ("tool_choice", "parallel_tool_calls", "text", "service_tier")
                        if key in request
                    }
                    data.model_config = {**(data.model_config or {}), **config} or None
            elif isinstance(data, ResponseSpanData):
                data.input = (
                    request.get("input", [request]) if isinstance(request, dict) else request
                )
        if "output" in event:
            output = bound_content(event["output"])
            if isinstance(data, GenerationSpanData):
                data.output = output
            elif isinstance(data, ResponseSpanData) and isinstance(event.get("response_id"), str):
                data.response = Response.model_construct(id=event["response_id"], output=output)
            if isinstance(event.get("response_id"), str):
                data.operation.response_id = event["response_id"]

    def _finish(self, key: tuple[str, str], outcome: str, at: str | None = None) -> None:
        span = self._active.pop(key)
        span.span_data.operation.outcome = outcome
        span.span_data.operation.estimated_end = at is None
        if outcome != "completed":
            span.set_error(
                {"message": "Native operation did not complete successfully.", "data": None}
            )
        if key[0] == "model":
            finish_model_span(span, ended_at=at)
        elif at is None:
            span.finish()
        else:
            span.finish_at(at)

    def close(self) -> None:
        self.drain()
        if self._thread is not None and self._expect_model and not self._saw_model:
            self._reason = self._reason or "missing_operations"
        for key in list(self._active):
            if key in self._ends:
                outcome, at = self._ends.pop(key)
                if self._capture:
                    self._content(
                        self._active[key],
                        key[0],
                        {"output": {"omitted": True, "reason": "output_unavailable"}},
                    )
                self._finish(key, outcome, at)
            else:
                self._reason = self._reason or "interrupted"
                self._finish(key, "interrupted")
        if self._reason is not None:
            with custom_span(
                "codex.tracing.coverage",
                {"coverage": "partial", "provider": "codex", "reason": self._reason},
                parent=self._parent,
            ):
                pass
