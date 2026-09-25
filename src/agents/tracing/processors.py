from __future__ import annotations

import json
import math
import os
import queue
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
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
    # 4xx statuses that are transient, mirroring the OpenAI client's own retry policy:
    # request timeout, conflict, and rate limiting.
    _RETRYABLE_CLIENT_STATUS_CODES = frozenset({408, 409, 429})
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

        # Keep a client open for connection pooling across multiple export calls
        self._client = httpx2.Client(timeout=httpx2.Timeout(timeout=60, connect=5.0))

    def set_api_key(self, api_key: str):
        """Set the OpenAI API key for the exporter.

        Args:
            api_key: The OpenAI API key to use. This is the same key used by the OpenAI Python
                client.
        """
        self._api_key = api_key

    @property
    def api_key(self):
        # Keep a key from the environment once it is found, but do not remember a missing one, so a
        # key that appears after an export without one, such as from a later `load_dotenv()`, is
        # still used. A lookup that finds nothing writes nothing, so it cannot discard a key that
        # `set_api_key()` stores while the lookup runs.
        api_key = self._api_key
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key:
                self._api_key = api_key
        return api_key

    @api_key.setter
    def api_key(self, api_key: str | None):
        # Assigning the attribute worked while it was a cached property, and callers rely on it.
        self._api_key = api_key

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
                    try:
                        # Encode the way the request body is encoded (UTF-8, no NaN), so
                        # strings holding unpaired surrogates are caught here as well.
                        json.dumps(exported, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    except (TypeError, ValueError):
                        logger.warning(
                            "[non-fatal] Tracing: sanitizing values that can't be sent as JSON."
                        )
                        exported = self._sanitize_json_compatible_value(exported)
                        # Strings pass the sanitizer unchanged, so replace any unpaired
                        # surrogates, which UTF-8 can't encode, with "?".
                        exported = json.loads(
                            json.dumps(exported, ensure_ascii=False)
                            .encode("utf-8", "replace")
                            .decode("utf-8")
                        )
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
            retry_after: float | None = None
            while True:
                retry_after = None
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

                    allows_retry = self._server_allows_retry(response)
                    # A rate limit, request timeout, or conflict is transient: retry it
                    # like a server error, waiting at least the advertised Retry-After,
                    # unless the server says outright not to retry.
                    if response.status_code in self._RETRYABLE_CLIENT_STATUS_CODES and allows_retry:
                        retry_after = self._retry_after_seconds(response)
                        logger.warning(
                            "[non-fatal] Tracing: client error %s, retrying.",
                            response.status_code,
                        )
                    # Any other client error (4xx) won't be retried
                    elif 400 <= response.status_code < 500:
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
                    elif not allows_retry:
                        logger.error(
                            "[non-fatal] Tracing: server forbade retry for %s.",
                            response.status_code,
                        )
                        break
                    else:
                        # For 5xx or other unexpected codes, treat it as transient and retry
                        logger.warning(
                            "[non-fatal] Tracing: server error %s, retrying.",
                            response.status_code,
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
                if retry_after is not None:
                    # Honour the server's wait, bounded by max_delay so a large
                    # Retry-After cannot stall the export thread.
                    sleep_time = min(max(sleep_time, retry_after), self.max_delay)
                if not self._sleep_before_retry(sleep_time, deadline):
                    break
                delay = min(delay * 2, self.max_delay)

    def _server_allows_retry(self, response: httpx2.Response) -> bool:
        # Mirror the OpenAI client: an explicit `x-should-retry: false` wins over the
        # status code classification.
        headers = getattr(response, "headers", None)
        if headers is None:
            return True
        should_retry = headers.get("x-should-retry")
        if not isinstance(should_retry, str):
            return True
        return should_retry.strip().lower() != "false"

    def _retry_after_seconds(self, response: httpx2.Response) -> float | None:
        # Imported lazily: the models package pulls in the OpenAI client, which must not
        # become an import-time dependency of tracing.
        from ..models._retry_runtime import parse_retry_after_ms, parse_retry_after_value

        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        retry_after = parse_retry_after_ms(headers.get("retry-after-ms"))
        if retry_after is None:
            retry_after = parse_retry_after_value(headers.get("retry-after"))
        if retry_after is None or not math.isfinite(retry_after):
            return None
        return retry_after

    def _timeout_for_deadline(self, deadline: float | None) -> httpx2.Timeout | None:
        if deadline is None:
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        connect_timeout = min(5.0, remaining)
        return httpx2.Timeout(remaining, connect=connect_timeout)

    def _sleep_before_retry(self, sleep_time: float, deadline: float | None) -> bool:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            logger.warning("[non-fatal] Tracing: export deadline reached before retry, giving up.")
            return False

        wait_for = sleep_time if remaining is None else min(sleep_time, remaining)
        if deadline is None and self._shutdown_event.wait(wait_for):
            logger.warning(
                "[non-fatal] Tracing: shutdown requested during retry backoff, giving up."
            )
            return False
        if deadline is not None:
            # The final drain runs after shutdown is requested; its deadline bounds retries.
            time.sleep(wait_for)
            if remaining is not None and (sleep_time >= remaining or time.monotonic() >= deadline):
                logger.warning(
                    "[non-fatal] Tracing: export deadline reached during retry backoff, giving up."
                )
                return False
        return True

    def _should_sanitize_for_openai_tracing_api(self) -> bool:
        return self.endpoint.rstrip("/") == self._OPENAI_TRACING_INGEST_ENDPOINT.rstrip("/")

    def _sanitize_for_openai_tracing_api(self, payload_item: dict[str, Any]) -> dict[str, Any]:
        """Omit reasoning from generation data and enforce traces ingest field limits.

        Original spans remain available to custom processors and exporters.
        """
        span_data = payload_item.get("span_data")
        if not isinstance(span_data, dict):
            return payload_item

        sanitized_span_data = span_data
        did_mutate = False

        for field_name in ("input", "output"):
            if field_name not in span_data:
                continue
            field_value = span_data[field_name]
            if span_data.get("type") == "generation":
                field_value = self._omit_generation_reasoning(field_value)
            sanitized_field = self._truncate_span_field_value(field_value)
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

    def _omit_generation_reasoning(self, value: Any) -> Any:
        """Filter SDK generation message shapes without traversing arbitrary user data."""
        if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
            return value

        def omit_message_reasoning(item: Any) -> Any:
            if not isinstance(item, Mapping):
                return item
            item = dict(item)
            if item.get("role") == "assistant":
                for key in ("reasoning", "reasoning_content", "thinking_blocks", "thinking"):
                    item.pop(key, None)
                content = item.get("content")
                if isinstance(content, list):
                    # Legacy thinking replay uses inline Anthropic content blocks.
                    item["content"] = [
                        part
                        for part in content
                        if not isinstance(part, dict)
                        or part.get("type") not in ("thinking", "redacted_thinking")
                    ]
            return item

        def omit_items(items: Sequence[Any]) -> list[Any]:
            # Third-party reasoning_content is also normalized into summary text.
            return [
                omit_message_reasoning(item)
                for item in items
                if not isinstance(item, Mapping) or item.get("type") != "reasoning"
            ]

        return [
            {**item, "output": omit_items(item["output"])}
            if isinstance(item, Mapping)
            and item.get("object") == "response"
            and isinstance(item.get("output"), list)
            else item
            for item in omit_items(value)
        ]

    def _value_json_size_bytes(self, value: Any) -> int:
        try:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return self._OPENAI_TRACING_MAX_FIELD_BYTES + 1
        # Unpaired surrogates can't be UTF-8 encoded; export replaces each with "?" later,
        # so count them as that one byte instead of raising.
        return len(serialized.encode("utf-8", "replace"))

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
                json_key = self._json_object_key(key)
                if json_key is None:
                    continue
                sanitized_value = self._sanitize_json_compatible_value(value)
                if sanitized_value is self._UNSERIALIZABLE:
                    continue
                details[json_key] = sanitized_value

        for key, value in usage.items():
            if key in self._OPENAI_TRACING_ALLOWED_USAGE_KEYS or key == "details" or value is None:
                continue
            json_key = self._json_object_key(key)
            if json_key is None:
                continue
            sanitized_value = self._sanitize_json_compatible_value(value)
            if sanitized_value is self._UNSERIALIZABLE:
                continue
            details[json_key] = sanitized_value

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
                    json_key = self._json_object_key(key)
                    if json_key is None:
                        continue
                    sanitized_nested = self._sanitize_json_compatible_value(nested_value, seen_ids)
                    if sanitized_nested is self._UNSERIALIZABLE:
                        continue
                    sanitized_dict[json_key] = sanitized_nested
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

    def _json_object_key(self, key: Any) -> str | None:
        """Return the key text json.dumps would send for ``key``, or None if it can't send it."""
        if isinstance(key, str):
            return key
        if key is None or isinstance(key, bool | int | float):
            try:
                # Same text json writes for these keys: "true", "null", "200", "1.5".
                return json.dumps(key, allow_nan=False)
            except ValueError:
                # A key the JSON encoder cannot represent, such as a non-finite float.
                return None
        return None

    def close(self):
        """Close the underlying HTTP client."""
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

    def _ensure_thread_started(self) -> None:
        # Fast path without holding the lock
        if self._worker_thread and self._worker_thread.is_alive():
            return

        # Double-checked locking to avoid starting multiple threads
        with self._thread_start_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return

            self._worker_thread = threading.Thread(target=self._run, daemon=True)
            self._worker_thread.start()

    def on_trace_start(self, trace: Trace) -> None:
        # Ensure the background worker is running before we enqueue anything.
        self._ensure_thread_started()

        try:
            self._queue.put_nowait(trace)
        except queue.Full:
            logger.warning("Queue is full, dropping trace.")

    def on_trace_end(self, trace: Trace) -> None:
        # We send traces via on_trace_start, so we don't need to do anything here.
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        # We send spans via on_span_end, so we don't need to do anything here.
        pass

    def on_span_end(self, span: Span[Any]) -> None:
        # Ensure the background worker is running before we enqueue anything.
        self._ensure_thread_started()

        try:
            self._queue.put_nowait(span)
        except queue.Full:
            logger.warning("Queue is full, dropping span.")

    def shutdown(self, timeout: float | None = None):
        """
        Called when the application stops. We signal our thread to stop, then join it.
        """
        self._shutdown_event.set()
        if timeout is not None:
            request_exporter_shutdown = getattr(self._exporter, "_request_shutdown", None)
            if callable(request_exporter_shutdown):
                request_exporter_shutdown()

        deadline = None if timeout is None else time.monotonic() + timeout
        self._shutdown_deadline = deadline

        # Only join if we ever started the background thread; otherwise flush synchronously.
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning(
                    "[non-fatal] Tracing: shutdown timeout reached; dropping queued traces."
                )
        else:
            # No background thread: process any remaining items synchronously.
            self._export_batches(deadline=deadline)

    def force_flush(self):
        """
        Forces an immediate flush of all queued spans.
        """
        self._export_batches()

    def _run(self):
        while not self._shutdown_event.is_set():
            current_time = time.monotonic()
            queue_size = self._queue.qsize()

            # If it's time for a scheduled flush or queue is above the trigger threshold
            if current_time >= self._next_export_time or queue_size >= self._export_trigger_size:
                self._export_batches()
                # Reset the next scheduled flush time
                self._next_export_time = time.monotonic() + self._schedule_delay
            else:
                # Sleep a short interval so we don't busy-wait.
                time.sleep(0.2)

        # Final drain after shutdown
        self._export_batches(deadline=self._shutdown_deadline)

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


def default_exporter() -> BackendSpanExporter:
    """The default exporter, which exports traces and spans to the backend in batches."""
    global _global_exporter

    exporter = _global_exporter
    if exporter is not None:
        return exporter

    with _global_lock:
        exporter = _global_exporter
        if exporter is None:
            exporter = BackendSpanExporter()
            _global_exporter = exporter

    return exporter


def default_processor() -> BatchTraceProcessor:
    """The default processor, which exports traces and spans to the backend in batches."""
    global _global_exporter
    global _global_processor

    processor = _global_processor
    if processor is not None:
        return processor

    with _global_lock:
        processor = _global_processor
        if processor is None:
            exporter = _global_exporter
            if exporter is None:
                exporter = BackendSpanExporter()
                _global_exporter = exporter
            processor = BatchTraceProcessor(exporter)
            _global_processor = processor

    return processor
