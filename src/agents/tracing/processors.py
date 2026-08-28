from __future__ import annotations

import json
import math
import os
import queue
import random
import threading
import time
from collections.abc import Callable
from functools import cached_property
from typing import Any, cast

import httpx2

from .. import _debug
from ..logger import (
    log_model_and_tool_action_error,
    log_model_and_tool_action_warning,
    logger,
)
from .processor_interface import TracingExporter, TracingProcessor
from .spans import Span
from .traces import Trace


class ConsoleSpanExporter(TracingExporter):
    """Prints the traces and spans to the console."""

    def export(self, items: list[Trace | Span[Any]]) -> None:
        for item in items:
            if _debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA:
                if isinstance(item, Trace):
                    print("[Exporter] Export trace. Trace data is redacted.")
                else:
                    print("[Exporter] Export span. Span data is redacted.")
                continue
            if isinstance(item, Trace):
                print(f"[Exporter] Export trace_id={item.trace_id}, name={item.name}")
            else:
                print(f"[Exporter] Export span: {item.export()}")


class BackendSpanExporter(TracingExporter):
    _OPENAI_TRACING_INGEST_ENDPOINT = "https://api.openai.com/v1/traces/ingest"
    _OPENAI_TRACING_MAX_FIELD_BYTES = 100_000
    _OPENAI_TRACING_STRING_TRUNCATION_SUFFIX = "... [truncated]"
    _OPENAI_TRACING_ALLOWED_USAGE_KEYS = frozenset(
        {
            "input_tokens",
            "output_tokens",
        }
    )
    _OPENAI_TRACING_USAGE_SPAN_TYPES = frozenset({"generation"})
    _UNSERIALIZABLE = object()

    def __init__(
        self,
        api_key: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        endpoint: str = _OPENAI_TRACING_INGEST_ENDPOINT,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ):
        """
        Args:
            api_key: The API key for the "Authorization" header. Defaults to
                `os.environ["OPENAI_API_KEY"]` if not provided.
            organization: The OpenAI organization to use. Defaults to
                `os.environ["OPENAI_ORG_ID"]` if not provided.
            project: The OpenAI project to use. Defaults to
                `os.environ["OPENAI_PROJECT_ID"]` if not provided.
            endpoint: The HTTP endpoint to which traces/spans are posted.
            max_retries: Maximum number of retries upon failures.
            base_delay: Base delay (in seconds) for the first backoff.
            max_delay: Maximum delay (in seconds) for backoff growth.
        """
        self._api_key = api_key
        self._organization = organization
        self._project = project
        self.endpoint = endpoint
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._shutdown_event = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False

        # Keep a client open for connection pooling across multiple export calls
        self._client = httpx2.Client(timeout=httpx2.Timeout(timeout=60, connect=5.0))

    def set_api_key(self, api_key: str):
        """Set the OpenAI API key for the exporter.

        Args:
            api_key: The OpenAI API key to use. This is the same key used by the OpenAI Python
                client.
        """
        # Clear the cached property if it exists
        if "api_key" in self.__dict__:
            del self.__dict__["api_key"]

        # Update the private attribute
        self._api_key = api_key

    @cached_property
    def api_key(self):
        return self._api_key or os.environ.get("OPENAI_API_KEY")

    @cached_property
    def organization(self):
        return self._organization or os.environ.get("OPENAI_ORG_ID")

    @cached_property
    def project(self):
        return self._project or os.environ.get("OPENAI_PROJECT_ID")

    def export(self, items: list[Trace | Span[Any]]) -> None:
        self._export_with_deadline(items, deadline=None)

    def _export_with_deadline(self, items: list[Trace | Span[Any]], deadline: float | None) -> None:
        if not items:
            return

        grouped_items: dict[str | None, list[Trace | Span[Any]]] = {}
        for item in items:
            key = item.tracing_api_key
            grouped_items.setdefault(key, []).append(item)

        for item_key, grouped in grouped_items.items():
            api_key = item_key or self.api_key
            if not api_key:
                logger.warning("OPENAI_API_KEY is not set, skipping trace export")
                continue

            sanitize_for_openai = self._should_sanitize_for_openai_tracing_api()
            data: list[dict[str, Any]] = []
            for item in grouped:
                exported = item.export()
                if exported:
                    if sanitize_for_openai:
                        exported = self._sanitize_for_openai_tracing_api(exported)
                    data.append(exported)
            payload = {"data": data}

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "traces=v1",
            }

            if self.organization:
                headers["OpenAI-Organization"] = self.organization

            if self.project:
                headers["OpenAI-Project"] = self.project

            # Exponential backoff loop
            attempt = 0
            delay = self.base_delay
            while True:
                request_timeout = self._timeout_for_deadline(deadline)
                if deadline is not None and request_timeout is None:
                    logger.warning(
                        "[non-fatal] Tracing: export deadline reached, giving up on this batch."
                    )
                    break

                attempt += 1
                try:
                    request_kwargs: dict[str, Any] = {
                        "url": self.endpoint,
                        "headers": headers,
                        "json": payload,
                    }
                    if request_timeout is not None:
                        request_kwargs["timeout"] = request_timeout
                    response = self._client.post(**request_kwargs)

                    # If the response is successful, break out of the loop
                    if response.status_code < 300:
                        logger.debug("Exported %s items", len(grouped))
                        break

                    # If the response is a client error (4xx), we won't retry
                    if 400 <= response.status_code < 500:
                        if _debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA:
                            logger.error(
                                "[non-fatal] Tracing client error %s. Response data is redacted.",
                                response.status_code,
                            )
                        else:
                            logger.error(
                                "[non-fatal] Tracing client error %s: %s",
                                response.status_code,
                                response.text,
                            )
                        break

                    # For 5xx or other unexpected codes, treat it as transient and retry
                    logger.warning(
                        "[non-fatal] Tracing: server error %s, retrying.", response.status_code
                    )
                except httpx2.RequestError as exc:
                    # Network or other I/O error, we'll retry
                    log_model_and_tool_action_warning(
                        logger, "[non-fatal] Tracing request failed", exc
                    )

                # If we reach here, we need to retry or give up
                if attempt >= self.max_retries:
                    logger.error(
                        "[non-fatal] Tracing: max retries reached, giving up on this batch."
                    )
                    break

                # Exponential backoff + jitter
                sleep_time = delay + random.uniform(0, 0.1 * delay)  # 10% jitter
                if not self._sleep_before_retry(sleep_time, deadline):
                    break
                delay = min(delay * 2, self.max_delay)

    def _timeout_for_deadline(self, deadline: float | None) -> httpx2.Timeout | None:
        if deadline is None:
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        connect_timeout = min(5.0, remaining)
        return httpx2.Timeout(remaining, connect=connect_timeout)

    def _sleep_before_retry(self, sleep_time: float, deadline: float | None) -> bool:
        if deadline is None:
            if self._shutdown_event.wait(sleep_time):
                logger.warning(
                    "[non-fatal] Tracing: shutdown requested during retry backoff, giving up."
                )
                return False
            return not self._shutdown_event.is_set()

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("[non-fatal] Tracing: export deadline reached before retry, giving up.")
            return False

        if sleep_time >= remaining:
            time.sleep(remaining)
            logger.warning(
                "[non-fatal] Tracing: export deadline reached during retry backoff, giving up."
            )
            return False

        time.sleep(sleep_time)
        return True

    def _should_sanitize_for_openai_tracing_api(self) -> bool:
        return self.endpoint.rstrip("/") == self._OPENAI_TRACING_INGEST_ENDPOINT.rstrip("/")

    def _sanitize_for_openai_tracing_api(self, payload_item: dict[str, Any]) -> dict[str, Any]:
        """Drop or truncate span fields known to be rejected by traces ingest."""
        span_data = payload_item.get("span_data")
        if not isinstance(span_data, dict):
            return payload_item

        sanitized_span_data = span_data
        did_mutate = False

        for field_name in ("input", "output"):
            if field_name not in span_data:
                continue
            sanitized_field = self._truncate_span_field_value(span_data[field_name])
            if sanitized_field is span_data[field_name]:
                continue
            if not did_mutate:
                sanitized_span_data = dict(span_data)
                did_mutate = True
            sanitized_span_data[field_name] = sanitized_field

        if span_data.get("type") not in self._OPENAI_TRACING_USAGE_SPAN_TYPES:
            if "usage" in span_data:
                if not did_mutate:
                    sanitized_span_data = dict(span_data)
                    did_mutate = True
                sanitized_span_data.pop("usage", None)
            if not did_mutate:
                return payload_item
            sanitized_payload_item = dict(payload_item)
            sanitized_payload_item["span_data"] = sanitized_span_data
            return sanitized_payload_item

        usage = span_data.get("usage")
        if not isinstance(usage, dict):
            if not did_mutate:
                return payload_item
            sanitized_payload_item = dict(payload_item)
            sanitized_payload_item["span_data"] = sanitized_span_data
            return sanitized_payload_item

        sanitized_usage = self._sanitize_generation_usage_for_openai_tracing_api(usage)

        if sanitized_usage is None:
            if not did_mutate:
                sanitized_span_data = dict(span_data)
                did_mutate = True
            sanitized_span_data.pop("usage", None)
        elif sanitized_usage != usage:
            if not did_mutate:
                sanitized_span_data = dict(span_data)
                did_mutate = True
            sanitized_span_data["usage"] = sanitized_usage

        if not did_mutate:
            return payload_item

        sanitized_payload_item = dict(payload_item)
        sanitized_payload_item["span_data"] = sanitized_span_data
        return sanitized_payload_item

    def _value_json_size_bytes(self, value: Any) -> int:
        try:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return self._OPENAI_TRACING_MAX_FIELD_BYTES + 1
        return len(serialized.encode("utf-8"))

    def _truncate_string_for_json_limit(self, value: str, max_bytes: int) -> str:
        value_size = self._value_json_size_bytes(value)
        if value_size <= max_bytes:
            return value

        suffix = self._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX
        suffix_size = self._value_json_size_bytes(suffix)
        if suffix_size > max_bytes:
            return ""
        if suffix_size == max_bytes:
            return suffix

        budget_without_suffix = max_bytes - suffix_size
        estimated_chars = int(len(value) * budget_without_suffix / max(value_size, 1))
        estimated_chars = max(0, min(len(value), estimated_chars))

        best = value[:estimated_chars] + suffix
        best_size = self._value_json_size_bytes(best)
        while best_size > max_bytes and estimated_chars > 0:
            overflow_ratio = (best_size - max_bytes) / max(best_size, 1)
            trim_chars = max(1, int(estimated_chars * overflow_ratio) + 1)
            estimated_chars = max(0, estimated_chars - trim_chars)
            best = value[:estimated_chars] + suffix
            best_size = self._value_json_size_bytes(best)

        return best

    def _truncate_span_field_value(self, value: Any) -> Any:
        max_bytes = self._OPENAI_TRACING_MAX_FIELD_BYTES
        if self._value_json_size_bytes(value) <= max_bytes:
            return value

        sanitized_value = self._sanitize_json_compatible_value(value)
        if sanitized_value is self._UNSERIALIZABLE:
            return self._truncated_preview(value)

        return self._truncate_json_value_for_limit(sanitized_value, max_bytes)

    def _truncate_json_value_for_limit(self, value: Any, max_bytes: int) -> Any:
        if self._value_json_size_bytes(value) <= max_bytes:
            return value

        if isinstance(value, str):
            return self._truncate_string_for_json_limit(value, max_bytes)

        if isinstance(value, dict):
            return self._truncate_mapping_for_json_limit(value, max_bytes)

        if isinstance(value, list):
            return self._truncate_list_for_json_limit(value, max_bytes)

        preview = self._truncated_preview(value)
        if self._value_json_size_bytes(preview) <= max_bytes:
            return preview

        return value

    def _truncate_mapping_for_json_limit(
        self, value: dict[str, Any], max_bytes: int
    ) -> dict[str, Any]:
        truncated = dict(value)
        current_size = self._value_json_size_bytes(truncated)

        while truncated and current_size > max_bytes:
            largest_key = max(
                truncated, key=lambda key: self._value_json_size_bytes(truncated[key])
            )
            child = truncated[largest_key]
            child_size = self._value_json_size_bytes(child)
            child_budget = max(0, max_bytes - (current_size - child_size))
            truncated_child = self._truncate_json_value_for_limit(child, child_budget)

            if truncated_child == child:
                truncated.pop(largest_key)
            else:
                truncated[largest_key] = truncated_child

            current_size = self._value_json_size_bytes(truncated)

        return truncated

    def _truncate_list_for_json_limit(self, value: list[Any], max_bytes: int) -> list[Any]:
        truncated = list(value)
        current_size = self._value_json_size_bytes(truncated)

        while truncated and current_size > max_bytes:
            largest_index = max(
                range(len(truncated)),
                key=lambda index: self._value_json_size_bytes(truncated[index]),
            )
            child = truncated[largest_index]
            child_size = self._value_json_size_bytes(child)
            child_budget = max(0, max_bytes - (current_size - child_size))
            truncated_child = self._truncate_json_value_for_limit(child, child_budget)

            if truncated_child == child:
                truncated.pop(largest_index)
            else:
                truncated[largest_index] = truncated_child

            current_size = self._value_json_size_bytes(truncated)

        return truncated

    def _truncated_preview(self, value: Any) -> dict[str, Any]:
        type_name = type(value).__name__
        preview = f"<{type_name} truncated>"
        if isinstance(value, dict):
            preview = f"<{type_name} len={len(value)} truncated>"
        elif isinstance(value, list | tuple | set | frozenset):
            preview = f"<{type_name} len={len(value)} truncated>"
        elif isinstance(value, bytes | bytearray | memoryview):
            preview = f"<{type_name} bytes={len(value)} truncated>"

        return {
            "truncated": True,
            "original_type": type_name,
            "preview": preview,
        }

    def _sanitize_generation_usage_for_openai_tracing_api(
        self, usage: dict[str, Any]
    ) -> dict[str, Any] | None:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not self._is_finite_json_number(input_tokens) or not self._is_finite_json_number(
            output_tokens
        ):
            return None

        details: dict[str, Any] = {}
        existing_details = usage.get("details")
        if isinstance(existing_details, dict):
            for key, value in existing_details.items():
                if not isinstance(key, str):
                    continue
                sanitized_value = self._sanitize_json_compatible_value(value)
                if sanitized_value is self._UNSERIALIZABLE:
                    continue
                details[key] = sanitized_value

        for key, value in usage.items():
            if key in self._OPENAI_TRACING_ALLOWED_USAGE_KEYS or key == "details" or value is None:
                continue
            sanitized_value = self._sanitize_json_compatible_value(value)
            if sanitized_value is self._UNSERIALIZABLE:
                continue
            details[key] = sanitized_value

        sanitized_usage: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if details:
            sanitized_usage["details"] = details
        return sanitized_usage

    def _is_finite_json_number(self, value: Any) -> bool:
        if isinstance(value, bool):
            return False
        return isinstance(value, int | float) and not (
            isinstance(value, float) and not math.isfinite(value)
        )

    def _sanitize_json_compatible_value(self, value: Any, seen_ids: set[int] | None = None) -> Any:
        if value is None or isinstance(value, str | bool | int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else self._UNSERIALIZABLE
        if seen_ids is None:
            seen_ids = set()
        if isinstance(value, dict):
            value_id = id(value)
            if value_id in seen_ids:
                return self._UNSERIALIZABLE
            seen_ids.add(value_id)
            sanitized_dict: dict[str, Any] = {}
            try:
                for key, nested_value in value.items():
                    if not isinstance(key, str):
                        continue
                    sanitized_nested = self._sanitize_json_compatible_value(nested_value, seen_ids)
                    if sanitized_nested is self._UNSERIALIZABLE:
                        continue
                    sanitized_dict[key] = sanitized_nested
            finally:
                seen_ids.remove(value_id)
            return sanitized_dict
        if isinstance(value, list | tuple):
            value_id = id(value)
            if value_id in seen_ids:
                return self._UNSERIALIZABLE
            seen_ids.add(value_id)
            sanitized_list: list[Any] = []
            try:
                for nested_value in value:
                    sanitized_nested = self._sanitize_json_compatible_value(nested_value, seen_ids)
                    if sanitized_nested is self._UNSERIALIZABLE:
                        continue
                    sanitized_list.append(sanitized_nested)
            finally:
                seen_ids.remove(value_id)
            return sanitized_list
        return self._UNSERIALIZABLE

    @property
    def is_shut_down(self) -> bool:
        """Whether shutdown or close made this exporter terminal."""
        return self._shutdown_event.is_set() or self._closed

    def close(self):
        """Close the underlying HTTP client exactly once."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._client.close()

    def _request_shutdown(self) -> None:
        self._shutdown_event.set()


class BatchTraceProcessor(TracingProcessor):
    """Some implementation notes:
    1. Using Queue, which is thread-safe.
    2. Using a background thread to export spans, to minimize any performance issues.
    3. Spans are stored in memory until they are exported.
    """

    def __init__(
        self,
        exporter: TracingExporter,
        max_queue_size: int = 8192,
        max_batch_size: int = 128,
        schedule_delay: float = 5.0,
        export_trigger_ratio: float = 0.7,
        *,
        _owns_exporter: bool = False,
    ):
        """
        Args:
            exporter: The exporter to use.
            max_queue_size: The maximum number of spans to store in the queue. After this, we will
                start dropping spans.
            max_batch_size: The maximum number of spans to export in a single batch.
            schedule_delay: The delay between checks for new spans to export.
            export_trigger_ratio: The ratio of the queue size at which we will trigger an export.
        """
        self._exporter = exporter
        self._owns_exporter = _owns_exporter
        self._queue: queue.Queue[Trace | Span[Any]] = queue.Queue(maxsize=max_queue_size)
        self._max_queue_size = max_queue_size
        self._max_batch_size = max_batch_size
        self._schedule_delay = schedule_delay
        self._shutdown_event = threading.Event()

        # The queue size threshold at which we export immediately.
        self._export_trigger_size = max(1, int(max_queue_size * export_trigger_ratio))

        # Track when we next *must* perform a scheduled export
        self._next_export_time = time.monotonic() + self._schedule_delay

        # We lazily start the background worker thread the first time a span/trace is queued.
        self._worker_thread: threading.Thread | None = None
        self._thread_start_lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._shutdown_deadline: float | None = None

    def _begin_shutdown(self) -> threading.Thread | None:
        """Close admission and return the worker that owns any accepted work."""
        with self._thread_start_lock:
            self._shutdown_event.set()
            return self._worker_thread

    def _enqueue(self, item: Trace | Span[Any], full_message: str) -> None:
        """Publish work atomically with lazy worker creation and shutdown admission."""
        with self._thread_start_lock:
            if self._shutdown_event.is_set():
                return
            if not self._worker_thread or not self._worker_thread.is_alive():
                self._worker_thread = threading.Thread(target=self._run, daemon=True)
                self._worker_thread.start()
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                logger.warning(full_message)

    def on_trace_start(self, trace: Trace) -> None:
        self._enqueue(trace, "Queue is full, dropping trace.")

    def on_trace_end(self, trace: Trace) -> None:
        # We send traces via on_trace_start, so we don't need to do anything here.
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        # We send spans via on_span_end, so we don't need to do anything here.
        pass

    def on_span_end(self, span: Span[Any]) -> None:
        self._enqueue(span, "Queue is full, dropping span.")

    @property
    def is_shut_down(self) -> bool:
        """Whether shutdown has started. Shutdown is terminal for this processor."""
        exporter_is_shut_down = getattr(self._exporter, "is_shut_down", False)
        return self._shutdown_event.is_set() or (
            self._owns_exporter and exporter_is_shut_down is True
        )

    def shutdown(self, timeout: float | None = None):
        """
        Called when the application stops. We signal our thread to stop, then join it.
        """
        worker_thread = self._begin_shutdown()
        if timeout is not None:
            request_exporter_shutdown = getattr(self._exporter, "_request_shutdown", None)
            if callable(request_exporter_shutdown):
                request_exporter_shutdown()

        deadline = None if timeout is None else time.monotonic() + timeout
        self._shutdown_deadline = deadline

        # Only join if we ever started the background thread; otherwise flush synchronously.
        if worker_thread is not None:
            if worker_thread.is_alive():
                worker_thread.join(timeout=timeout)
                if worker_thread.is_alive():
                    logger.warning(
                        "[non-fatal] Tracing: shutdown timeout reached; dropping queued traces."
                    )
                    return
        else:
            # No background thread: process any remaining items synchronously.
            self._export_batches(deadline=deadline)

        self._close_owned_exporter()

    def force_flush(self):
        """
        Forces an immediate flush of all queued spans.
        """
        if not self._shutdown_event.is_set():
            self._export_batches()

    def _run(self):
        try:
            while not self._shutdown_event.is_set():
                current_time = time.monotonic()
                queue_size = self._queue.qsize()

                # If it's time for a scheduled flush or queue is above the trigger threshold
                if (
                    current_time >= self._next_export_time
                    or queue_size >= self._export_trigger_size
                ):
                    self._export_batches()
                    # Reset the next scheduled flush time
                    self._next_export_time = time.monotonic() + self._schedule_delay
                else:
                    # Sleep a short interval so we don't busy-wait.
                    time.sleep(0.2)

            # Final drain after shutdown
            self._export_batches(deadline=self._shutdown_deadline)
        finally:
            self._close_owned_exporter()

    def _close_owned_exporter(self) -> None:
        """Close only the exporter owned by the module-created default processor."""
        if not self._owns_exporter:
            return
        close = getattr(self._exporter, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:
            log_model_and_tool_action_error(
                logger, "[non-fatal] Tracing: error closing exporter on shutdown", exc
            )

    def _export_batches(self, deadline: float | None = None):
        """Drains the queue and exports in batches of up to `max_batch_size` until the queue
        is completely empty.
        """
        with self._export_lock:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    logger.warning(
                        "[non-fatal] Tracing: export deadline reached; dropping queued traces."
                    )
                    break

                items_to_export: list[Span[Any] | Trace] = []

                # Gather a batch of spans up to max_batch_size
                while not self._queue.empty() and len(items_to_export) < self._max_batch_size:
                    try:
                        items_to_export.append(self._queue.get_nowait())
                    except queue.Empty:
                        # Another thread might have emptied the queue between checks
                        break

                # If we collected nothing, we're done
                if not items_to_export:
                    break

                # Export the batch. Catch any exception so a misbehaving exporter
                # cannot kill the background worker thread and silently strand all
                # subsequent spans in the queue.
                try:
                    export_with_deadline = getattr(self._exporter, "_export_with_deadline", None)
                    if deadline is not None and callable(export_with_deadline):
                        export_fn = cast(
                            Callable[[list[Trace | Span[Any]], float | None], None],
                            export_with_deadline,
                        )
                        export_fn(items_to_export, deadline)
                    else:
                        self._exporter.export(items_to_export)
                except Exception as exc:
                    log_model_and_tool_action_error(
                        logger,
                        (
                            "[non-fatal] Tracing exporter failed; "
                            f"dropping batch of {len(items_to_export)} items"
                        ),
                        exc,
                    )


# Lazily initialized defaults to avoid creating network clients or threading
# primitives during module import (important for fork-based process models).
_global_exporter: BackendSpanExporter | None = None
_global_processor: BatchTraceProcessor | None = None
_global_lock = threading.Lock()
_DEFAULT_STACK_ATEXIT_SHUTDOWN = False
_global_exporter_attached = False
_retirement_lock = threading.Lock()
_retiring_default_processors: dict[BatchTraceProcessor, threading.Thread] = {}


def _begin_default_stack_atexit_shutdown() -> None:
    """Prevent a later atexit callback from rebuilding terminal defaults."""
    global _DEFAULT_STACK_ATEXIT_SHUTDOWN

    with _global_lock:
        _DEFAULT_STACK_ATEXIT_SHUTDOWN = True


def _close_unattached_default_exporter() -> None:
    """Close a configured default exporter that never gained an owning processor."""
    with _global_lock:
        if _global_processor is not None or _global_exporter_attached:
            return
        exporter = _global_exporter

    if exporter is not None:
        try:
            exporter.close()
        except Exception as exc:
            log_model_and_tool_action_error(
                logger, "[non-fatal] Tracing: error closing unattached exporter", exc
            )


def _wait_for_retiring_default_processors(timeout: float | None) -> None:
    """Give detached SDK defaults the same bounded atexit drain window."""
    deadline = None if timeout is None else time.monotonic() + timeout
    with _retirement_lock:
        threads = tuple(_retiring_default_processors.values())

    for thread in threads:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining is not None and remaining <= 0:
            return
        thread.join(timeout=remaining)


def _replacement_exporter(exporter: BackendSpanExporter) -> BackendSpanExporter:
    """Create a live default exporter with the prior default exporter's configuration."""
    return BackendSpanExporter(
        api_key=exporter._api_key,
        organization=exporter._organization,
        project=exporter._project,
        endpoint=exporter.endpoint,
        max_retries=exporter.max_retries,
        base_delay=exporter.base_delay,
        max_delay=exporter.max_delay,
    )


def _replace_shut_down_defaults() -> None:
    """Replace terminal cached defaults while retaining their configured values.

    Callers must hold the default-stack lock.
    """
    global _global_exporter
    global _global_processor
    global _global_exporter_attached

    processor = _global_processor
    exporter = _global_exporter
    if processor is not None and processor.is_shut_down:
        if not processor._shutdown_event.is_set():
            processor.shutdown(timeout=0.0)
        _global_processor = None
        if exporter is not None:
            _global_exporter = _replacement_exporter(exporter)
            _global_exporter_attached = False
        return
    if exporter is not None and exporter.is_shut_down:
        if processor is not None:
            processor.shutdown(timeout=0.0)
        _global_exporter = _replacement_exporter(exporter)
        _global_processor = None
        _global_exporter_attached = False


def default_exporter() -> BackendSpanExporter:
    """The default exporter, which exports traces and spans to the backend in batches."""
    global _global_exporter

    with _global_lock:
        if _DEFAULT_STACK_ATEXIT_SHUTDOWN and _global_exporter is None:
            raise RuntimeError("Tracing shutdown has started; default exporter is unavailable.")
        if not _DEFAULT_STACK_ATEXIT_SHUTDOWN:
            _replace_shut_down_defaults()
        if _global_exporter is None:
            _global_exporter = BackendSpanExporter()
        return _global_exporter


def _set_default_exporter_api_key(api_key: str) -> None:
    """Update the active default exporter atomically with terminal replacement."""
    global _global_exporter

    with _global_lock:
        if _DEFAULT_STACK_ATEXIT_SHUTDOWN:
            return
        _replace_shut_down_defaults()
        if _global_exporter is None:
            _global_exporter = BackendSpanExporter()
        _global_exporter.set_api_key(api_key)


def default_processor() -> BatchTraceProcessor:
    """The default processor, which exports traces and spans to the backend in batches."""
    global _global_exporter
    global _global_processor
    global _global_exporter_attached

    with _global_lock:
        if _DEFAULT_STACK_ATEXIT_SHUTDOWN and _global_processor is None:
            raise RuntimeError("Tracing shutdown has started; default processor is unavailable.")
        if not _DEFAULT_STACK_ATEXIT_SHUTDOWN:
            _replace_shut_down_defaults()
        if _global_processor is None:
            if _global_exporter is None:
                _global_exporter = BackendSpanExporter()
            _global_processor = BatchTraceProcessor(_global_exporter, _owns_exporter=True)
            _global_exporter_attached = True
        return _global_processor


def _detach_owned_default_processor(processor: TracingProcessor) -> None:
    """Publish a fresh cache and close admission on the retired default."""
    global _global_exporter
    global _global_processor
    global _global_exporter_attached

    if not isinstance(processor, BatchTraceProcessor) or not processor._owns_exporter:
        return

    with _global_lock:
        if _global_processor is processor:
            exporter = _global_exporter
            _global_processor = None
            if exporter is processor._exporter:
                _global_exporter = _replacement_exporter(exporter)
            _global_exporter_attached = False
        processor._begin_shutdown()


def _retire_owned_default_processor(processor: TracingProcessor) -> None:
    """Drain an SDK-owned default asynchronously without delaying reconfiguration."""
    if not isinstance(processor, BatchTraceProcessor) or not processor._owns_exporter:
        return

    _detach_owned_default_processor(processor)

    def retire() -> None:
        try:
            processor.shutdown(timeout=None)
        finally:
            with _retirement_lock:
                _retiring_default_processors.pop(processor, None)

    thread = threading.Thread(target=retire, daemon=True)
    with _retirement_lock:
        if processor in _retiring_default_processors:
            return
        _retiring_default_processors[processor] = thread
        thread.start()
