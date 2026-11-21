"""Trace stock Codex request and tool callbacks without copying native payloads."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..tracing import (
    Span,
    custom_span,
    function_span,
    get_current_span,
    get_current_trace,
    response_span,
)
from ..tracing._model import acquire_model_span, finish_model_span
from ..tracing.config import get_operation_detail
from ..tracing.provider import _is_noop_span
from ..usage import _response_usage_to_usage, model_usage_to_span_usage
from ._codex_usage import _usage
from .interface import ModelTracing


class CodexOperationTracing:
    """Own observations for one ephemeral Codex thread and one SDK model attempt."""

    def __init__(self, sdk_tools: set[tuple[str | None, str]]) -> None:
        self._sdk_tools = sdk_tools
        self._parent = get_current_span() or get_current_trace()
        self._thread: Any = None
        self._active: dict[tuple[str, str], Span[Any]] = {}
        self._reason: str | None = None
        self._saw_model = False

    def bind(self, thread: Any, tracing: ModelTracing) -> None:
        if tracing.is_disabled() or self._parent is None:
            return
        # A hook can disable the provider after the parent starts.
        if _is_noop_span(response_span(parent=self._parent)):
            return
        if get_operation_detail(include_data=tracing.include_data()) == "content":
            self._reason = "metadata_only"
        try:
            thread.enable_tracing()
            self._thread = thread
        except Exception:
            self._reason = "unavailable"

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
            # Observation failure must never change the result or cause a retry.
            self._reason = "telemetry_error"

    def _observe(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        if kind == "dropped":
            self._reason = "dropped_operations"
            return
        if kind == "tool" and (event.get("namespace"), event["name"]) in self._sdk_tools:
            # Runner owns the approval and execution spans for SDK tools.
            return
        key = kind, event["id"]
        at = datetime.fromtimestamp(event["at"] / 1000, tz=timezone.utc).isoformat()
        if event["phase"] == "start":
            if key in self._active or len(self._active) >= 128:
                self._reason = "dropped_operations"
                return
            span: Span[Any]
            if kind == "model":
                span = acquire_model_span(
                    ModelTracing.ENABLED_WITHOUT_DATA,
                    event["model"],
                    parent=self._parent,
                    started_at=at,
                )
                self._saw_model = True
            elif kind == "tool":
                name = event["name"]
                if event.get("namespace"):
                    name = f"{event['namespace']}.{name}"
                span = function_span(name, parent=self._parent)
                span.span_data.operation.detail = get_operation_detail(include_data=False)
                span.span_data.operation.tool_call_id = event["id"]
                span.start_at(at)
            else:
                raise ValueError("Unknown Codex operation.")
            self._active[key] = span
            return

        if key not in self._active:
            # Core can block a call before on_tool_start. Never invent an execution.
            self._reason = "missing_start"
            return
        if kind == "model" and event.get("usage") is not None:
            self._active[key].span_data.usage = model_usage_to_span_usage(
                _response_usage_to_usage(_usage(event["usage"]))
            )
        self._finish(key, event["outcome"], at)

    def _finish(self, key: tuple[str, str], outcome: str, at: str | None = None) -> None:
        span = self._active.pop(key)
        span.span_data.operation.outcome = outcome
        span.span_data.operation.estimated_end = at is None
        if outcome != "completed":
            span.set_error(
                {"message": "Codex operation did not complete successfully.", "data": None}
            )
        if key[0] == "model":
            finish_model_span(span, ended_at=at)
        elif at is None:
            span.finish()
        else:
            span.finish_at(at)

    def close(self) -> None:
        self.drain()
        if self._thread is not None and not self._saw_model:
            self._reason = self._reason or "missing_operations"
        for key in list(self._active):
            self._reason = self._reason or "interrupted"
            self._finish(key, "interrupted")
        if self._reason is not None:
            with custom_span(
                "codex.tracing.coverage",
                {"coverage": "partial", "provider": "codex", "reason": self._reason},
                parent=self._parent,
            ):
                pass
