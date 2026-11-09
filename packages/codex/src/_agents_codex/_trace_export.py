"""OpenAI traces-ingest serialization for real native lifecycle snapshots."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from time import monotonic
from typing import Any, TextIO
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


class OpenAITraceExporter:
    """Export native lifecycle records to the OpenAI traces-ingest endpoint.

    Pass an instance in configure(processors=[...]). Native flush barriers also
    wait for these deliveries. Each completed native span becomes a custom
    dashboard span with its native attributes, IDs and exact timestamps. The
    Install ``openai-agents[tracing]`` to use this HTTP exporter. Each delivery
    awaits one client and its transport cleanup within the existing processor
    worker loop. No client is shared across loops and no background queue or
    Classic span objects are created. Failures are reported by the native
    processor delivery/flush counters.
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
        try:
            address = urlsplit(endpoint)
            _ = address.port
        except (TypeError, ValueError):
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
            raise ImportError("Install openai-agents[tracing] to use OpenAITraceExporter") from None
        self._httpx = httpx2
        self._headers = {"Content-Type": "application/json", "OpenAI-Beta": "traces=v1"}
        self.set_api_key(api_key)
        for name, value in (("OpenAI-Organization", organization), ("OpenAI-Project", project)):
            if value is not None:
                self._validate_header(value)
                self._headers[name] = value
        self._address = address
        self._timeout = timeout_ms / 1000
        self._max_retries = max_retries
        self._traces: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _validate_header(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise ValueError("Trace export headers must be nonempty printable ASCII strings")

    def set_api_key(self, api_key: str) -> None:
        """Replace credentials for subsequent deliveries; never include them in records."""
        self._validate_header(api_key)
        self._headers["Authorization"] = "Bearer " + api_key

    async def on_trace_event(self, event: Mapping[str, Any]) -> None:
        trace_id = "trace_" + event["trace_id"]
        records: list[dict[str, Any]] = []
        if trace_id not in self._traces:
            records.append(
                {
                    "object": "trace",
                    "id": trace_id,
                    "workflow_name": event["name"],
                    "group_id": event["attributes"].get("sdk.group_id"),
                    "metadata": {"runtime": "native"},
                }
            )
        if event["lifecycle"] == "end":
            records.append(
                {
                    "object": "trace.span",
                    "id": _span_id(event["span_id"]),
                    "trace_id": trace_id,
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
        if not records:
            return
        payload = json.dumps({"data": records}, allow_nan=False).encode()
        deadline = monotonic() + self._timeout
        try:
            await asyncio.wait_for(self._send(payload, dict(self._headers)), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Native OpenAI trace export failed") from None
        if monotonic() > deadline:
            raise RuntimeError("Native OpenAI trace export failed") from None
        self._traces[trace_id] = None
        self._traces.move_to_end(trace_id)
        if len(self._traces) > 1024:
            self._traces.popitem(last=False)

    async def _send(self, payload: bytes, headers: dict[str, str]) -> None:
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
