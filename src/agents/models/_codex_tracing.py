"""Translate bounded native observations without affecting model or tool execution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from openai.types.responses import Response

from ..items import TResponseInputItem
from ..tracing import (
    Span,
    custom_span,
    function_span,
    get_current_span,
    get_current_trace,
    response_span,
)
from ..tracing._content import _json, bound_content
from ..tracing._model import (
    _add_usage,
    acquire_model_span,
    finish_model_span,
    record_model_input,
    record_retries,
    restart_model_span,
)
from ..tracing.config import get_operation_detail
from ..tracing.provider import _is_noop_span
from ..usage import _response_usage_to_usage, model_usage_to_span_usage
from ._codex_usage import _usage
from .interface import ModelTracing


class CodexOperationTracing:
    """Own observations for one ephemeral native thread, including tool restoration."""

    def __init__(
        self,
        sdk_tools: set[tuple[str | None, str]],
        input: str | list[TResponseInputItem] | None = None,
        *,
        expect_model: bool = True,
    ) -> None:
        # Core reports bare functions in its default "functions" namespace.
        self._sdk_tools = {(namespace or "functions", name) for namespace, name in sdk_tools}
        self._input = input
        self._expect_model = expect_model
        self._parent = get_current_span() or get_current_trace()
        self._thread: Any = None
        self._active: dict[tuple[str, str], Span[Any]] = {}
        self._ends: dict[tuple[str, str], tuple[str, str]] = {}
        self._ignored: set[str] = set()
        self._reason: str | None = None
        self._first_model: Span[Any] | None = None
        self._last_model: tuple[Span[Any], str] | None = None
        self._identity: str | None = None
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
        self._tracing = tracing
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
        namespace = event.get("namespace") or "functions"
        if kind == "tool":
            if phase == "start" and (namespace, event["name"]) in self._sdk_tools:
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
                span = self._start_model(event, at)
                if self._first_model is None:
                    self._first_model = span
            elif kind == "tool":
                name = event["name"]
                if namespace != "functions":
                    name = f"{namespace}.{name}"
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
            if phase == "retry":
                record_retries(event["count"], span=span, at=at)
                return
            data = span.span_data
            if event.get("usage") is not None:
                usage = model_usage_to_span_usage(_response_usage_to_usage(_usage(event["usage"])))
                data.usage = _add_usage(data.usage or {}, usage)
            if (
                data.operation.detail is None
                and self._tracing.include_data()
                and span is self._first_model
                and event["outcome"] == "completed"
            ):
                data.input = self._input
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
        if kind == "tool":
            for direction in ("input", "output"):
                if direction in event:
                    value = bound_content(event[direction])
                    setattr(data, direction, value if isinstance(value, str) else _json(value))
            return
        if "input" in event:
            request = event["input"]
            record_model_input(data, {"input": [request]} if request.get("omitted") else request)
        if "output" in event:
            output = bound_content(event["output"])
            data.output = [output] if isinstance(output, dict) else output
            data.operation.response_id = event.get("response_id")

    def _start_model(self, event: dict[str, Any], at: str) -> Span[Any]:
        previous, self._last_model = self._last_model, None
        identity = event.get("identity")
        # A failed request can resume on another stream or transport. A tool
        # result or a successful inference always starts a new logical call.
        if (
            previous
            and previous[0].span_data.operation.outcome == "failed"
            and identity is not None
            and identity == self._identity
        ):
            span = previous[0]
            restart_model_span(span, span.span_data.operation, at=at)
        else:
            if previous:
                finish_model_span(previous[0], ended_at=previous[1])
            span = acquire_model_span(
                self._tracing, event["model"], parent=self._parent, started_at=at
            )
        self._identity = identity
        span.span_data.operation.operation_id = event["id"]
        self._content(span, "model", event)
        return span

    def _finish(self, key: tuple[str, str], outcome: str, at: str | None = None) -> None:
        span = self._active.pop(key)
        span.span_data.operation.outcome = outcome
        span.span_data.operation.estimated_end = at is None
        if outcome != "completed":
            span.set_error(
                {"message": "Native operation did not complete successfully.", "data": None}
            )
        if key[0] == "model" and at is not None:
            # The next request identifies a retry. The final successful span
            # can carry the caller-visible response under the default policy.
            self._last_model = (span, at)
        elif key[0] == "model":
            finish_model_span(span)
        elif at is None:
            span.finish()
        else:
            span.finish_at(at)

    def close(self, response: Response | None = None) -> None:
        self.drain()
        if self._last_model:
            span, at = self._last_model
            operation = span.span_data.operation
            if (
                operation.detail is None
                and self._tracing.include_data()
                and operation.outcome == "completed"
            ):
                span.span_data.response = response
            finish_model_span(span, ended_at=at)
        if self._thread is not None and self._expect_model and self._first_model is None:
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
