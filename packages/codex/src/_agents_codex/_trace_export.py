"""OpenAI traces-ingest serialization for real native lifecycle snapshots."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any, TextIO, cast
from urllib.parse import urlsplit

from ._public import preserve_module_identity as _preserve_module_identity


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _timestamp(nanos: str) -> str:
    seconds, fraction = divmod(int(nanos), 1_000_000_000)
    return (
        datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{fraction:09d}Z"
    )


def _span_id(value: str | None) -> str | None:
    # The ingest format uses 24 hex digits; zero-extension preserves native IDs.
    return None if value is None else "span_" + value.rjust(24, "0")


@dataclass
class _TraceState:
    descriptor: dict[str, Any]
    active_spans: int = 0
    root_ended: bool = False
    delivered_to: object | None = None


def _descriptor(event: Mapping[str, Any]) -> dict[str, Any]:
    metadata = {
        key.removeprefix("sdk.metadata."): _plain(value)
        for key, value in event["attributes"].items()
        if key.startswith("sdk.metadata.")
    }
    metadata["runtime"] = "native"
    return {
        "object": "trace",
        "id": "trace_" + event["trace_id"],
        "workflow_name": event["name"],
        "group_id": event["attributes"].get("sdk.group_id"),
        "metadata": metadata,
    }


class OpenAITraceExporter:
    """Export native lifecycle records to the OpenAI traces-ingest endpoint.

    Pass an instance in configure(processors=[...]). Native flush barriers also
    wait for these deliveries. Each completed native span becomes a custom
    dashboard span with its native attributes, IDs and exact timestamps.
    Install ``openai-agents[tracing]`` to use this HTTP exporter. Each delivery
    awaits one client and its transport cleanup within the existing processor
    worker loop. No client is shared across loops and no background queue or
    Classic span objects are created. Failures are reported by the native
    processor delivery/flush counters. Admitted records in a native-drain batch are
    delivered as one HTTP request before any unrelated admission error is reported;
    native acknowledgement waits for that request. Up to 1024 recent
    trace descriptors are retained. Active roots and descendants prevent eviction;
    completed traces are retired first. Retained native contexts have been supplying
    original closed-root descriptors after ordinary eviction. New roots beyond active capacity fail
    delivery, and unknown child-only records never fabricate a root. The metadata
    runtime key is reserved for the native marker; other flattened native
    sdk.metadata attributes are preserved without guessing their JSON types.
    After native record loss, the SDK drains through a captured native sequence
    watermark then invalidates uncertain descriptors. Only fresh root starts can
    re-establish identity; old child/end records fail closed. Native flush keeps
    reporting loss. Lossless active roots retain their original eviction guarantee.
    """

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.openai.com/v1/traces/ingest",
        organization: str | None = None,
        project: str | None = None,
        timeout_ms: int = 750,
        max_retries: int = 0,
    ) -> None:
        value = None
        address = None
        headers = None
        try:
            try:
                address = urlsplit(endpoint)
                _ = address.port
            except (TypeError, ValueError):
                address = None
            if address is None:
                raise ValueError("Invalid trace export endpoint") from None
            if (
                address.scheme not in ("http", "https")
                or not address.hostname
                or address.username
                or address.password
                or address.fragment
            ):
                raise ValueError(
                    "Trace endpoint must be an HTTP(S) URL without credentials or fragments"
                )
            if (
                isinstance(timeout_ms, bool)
                or not isinstance(timeout_ms, int)
                or not 1 <= timeout_ms <= 900
            ):
                raise ValueError("timeout_ms must be an integer from 1 through 900")
            if (
                isinstance(max_retries, bool)
                or not isinstance(max_retries, int)
                or not 0 <= max_retries <= 3
            ):
                raise ValueError("max_retries must be an integer from 0 through 3")
            try:
                import httpx2
            except ImportError:
                raise ImportError(
                    "Install openai-agents[tracing] to use OpenAITraceExporter"
                ) from None
            self._httpx = httpx2
            headers = {"Content-Type": "application/json", "OpenAI-Beta": "traces=v1"}
            for name, value in (("OpenAI-Organization", organization), ("OpenAI-Project", project)):
                if value is not None:
                    self._validate_header(value)
                    headers[name] = value
            self._validate_header(api_key)
            self._address = address
            self._timeout = timeout_ms / 1000
            self._max_retries = max_retries
            self._credentials = (object(), headers)
            self.set_api_key(api_key)
            self._traces: OrderedDict[str, _TraceState] = OrderedDict()
            self._requires_root_start = False
        finally:
            del api_key, endpoint, organization, project, value, address, headers

    @staticmethod
    def _validate_header(value: str) -> None:
        invalid = False
        try:
            invalid = (
                not isinstance(value, str)
                or not value
                or any(ord(char) < 32 or ord(char) > 126 for char in value)
            )
        finally:
            del value
        if invalid:
            raise ValueError("Trace export headers must be nonempty printable ASCII strings")

    def set_api_key(self, api_key: str) -> None:
        """Replace credentials for later deliveries and resend their trace descriptors."""
        try:
            self._validate_header(api_key)
            # One immutable assignment keeps worker admission coherent with rotation.
            self._credentials = (
                object(),
                {**self._credentials[1], "Authorization": "Bearer " + api_key},
            )
        finally:
            del self, api_key

    def _on_native_trace_loss(self) -> None:
        # A lost end invalidates liveness; never guess which active roots survived.
        self._traces.clear()
        self._requires_root_start = True

    def _prepare_events(
        self, events: Sequence[Mapping[str, Any]], revision: object
    ) -> tuple[list[dict[str, Any]], list[tuple[str, _TraceState, dict[str, Any]]], str | None]:
        states: dict[str, _TraceState] = {}
        admitted: list[Mapping[str, Any]] = []
        admission_error = None
        for event in events:
            trace_id = "trace_" + event["trace_id"]
            root = event.get("parent_span_id") is None
            state = self._traces.get(trace_id)
            if state is None:
                retained = event.get("root_descriptor") if not self._requires_root_start else None
                if (not root and retained is None) or (
                    self._requires_root_start and (not root or event["lifecycle"] != "start")
                ):
                    admission_error = (
                        admission_error or "Native trace root descriptor is unavailable"
                    )
                    continue
                if len(self._traces) >= 1024:
                    completed = next(
                        (
                            key
                            for key, entry in self._traces.items()
                            if entry.root_ended and entry.active_spans == 0
                        ),
                        None,
                    )
                    if completed is None:
                        admission_error = (
                            admission_error or "Native active trace descriptor capacity reached"
                        )
                        continue
                    del self._traces[completed]
                source = (
                    event
                    if root
                    else {**cast(Mapping[str, Any], retained), "trace_id": event["trace_id"]}
                )
                state = _TraceState(_descriptor(source), root_ended=not root)
                self._traces[trace_id] = state
            if event["lifecycle"] == "start":
                state.active_spans += 1
            else:
                state.active_spans = max(0, state.active_spans - 1)
                if root:
                    state.root_ended = True
            self._traces.move_to_end(trace_id)
            states[trace_id] = state
            admitted.append(event)
        # Native acknowledgement covers the whole batch. Preserve valid traces
        # even when unrelated records have lost their root descriptor.
        pending = [
            (trace_id, state, state.descriptor)
            for trace_id, state in states.items()
            if state.delivered_to is not revision
        ]
        records = [descriptor for _, _, descriptor in pending]
        for event in admitted:
            if event["lifecycle"] == "end":
                records.append(
                    {
                        "object": "trace.span",
                        "id": _span_id(event["span_id"]),
                        "trace_id": "trace_" + event["trace_id"],
                        "parent_id": _span_id(event["parent_span_id"]),
                        "started_at": _timestamp(event["start_time_unix_nanos"]),
                        "ended_at": _timestamp(event["end_time_unix_nanos"]),
                        "span_data": {
                            "type": "custom",
                            "name": event["name"],
                            "data": _plain(event["attributes"]),
                        },
                        "error": {
                            "message": "Native span failed",
                            "data": {"outcome": event["outcome"]},
                        }
                        if event["outcome"] == "error"
                        else None,
                    }
                )
        return records, pending, admission_error

    async def on_trace_event(self, event: Mapping[str, Any]) -> None:
        try:
            await self.on_trace_events((event,))
        finally:
            del self, event

    async def on_trace_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        """Deliver one native-owned batch before its records may be acknowledged."""
        revision, headers = self._credentials
        records: list[dict[str, Any]] = []
        pending: list[tuple[str, _TraceState, dict[str, Any]]] = []
        payload = b""
        deadline = monotonic() + self._timeout
        cancelled: asyncio.CancelledError | None = None
        failed = False
        admission_error = None
        delivery_failed = False
        trace_id = state = descriptor = None
        try:
            try:
                records, pending, admission_error = self._prepare_events(events, revision)
                if records:
                    payload = json.dumps({"data": records}, allow_nan=False).encode()
                    await asyncio.wait_for(
                        self._send(payload, dict(headers)), timeout=max(0.0, deadline - monotonic())
                    )
            except asyncio.CancelledError as error:
                # Preserve cancellation identity/args without completed transport frames.
                cancelled = error.with_traceback(None)
                cancelled.__cause__ = None
                cancelled.__context__ = None
            except Exception:
                failed = True
            delivery_failed = failed or (bool(records) and monotonic() > deadline)
            if cancelled is None and not delivery_failed:
                if self._credentials[0] is revision:
                    for trace_id, state, descriptor in pending:
                        if self._traces.get(trace_id) is state and state.descriptor is descriptor:
                            state.delivered_to = revision
                if admission_error is None:
                    return
        finally:
            del self, events, payload, records, pending, headers, trace_id, state, descriptor
        if cancelled is not None:
            raise cancelled from None
        if admission_error is not None:
            message = "Native OpenAI trace export failed: record admission failed"
            if delivery_failed:
                message += "; delivery failed"
            raise RuntimeError(message) from None
        raise RuntimeError("Native OpenAI trace export failed") from None

    async def _send(self, payload: bytes, headers: dict[str, str]) -> None:
        client = response = None
        try:
            # Keep the client inside the delivery's timeout and cancellation scope.
            # Streaming reads only headers; remote response bodies are never retained.
            async with self._httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False, trust_env=False
            ) as client:
                for attempt in range(self._max_retries + 1):
                    retry = False
                    try:
                        async with client.stream(
                            "POST", self._address.geturl(), content=payload, headers=headers
                        ) as response:
                            if 200 <= response.status_code < 300:
                                return
                            retry = response.status_code >= 500
                    except self._httpx.HTTPError:
                        retry = True
                    if not retry or attempt == self._max_retries:
                        break
            # Avoid exposing endpoints, request bodies, response bodies or credentials.
            raise RuntimeError("Native OpenAI trace export failed") from None
        finally:
            del self, payload, headers, client, response


class ConsoleTraceProcessor:
    """Write native lifecycle diagnostics as JSON lines to stdout or a supplied stream.

    Native capture policy determines record contents. Explicitly enabled content
    may appear in these diagnostics; credentials are never part of native records.
    """

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream

    def on_trace_event(self, event: Mapping[str, Any]) -> None:
        import sys

        stream = self._stream if self._stream is not None else sys.stdout
        stream.write(json.dumps(_plain(event), allow_nan=False) + "\n")
        stream.flush()


_preserve_module_identity(globals(), "agents.telemetry")
