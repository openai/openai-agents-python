"""Optional native telemetry for an SDK process.

Await configure() before opening any Sessions. Close all Sessions before
awaiting shutdown(). Native owns the exporters and the process lifetime.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias

__all__ = ["OtelTlsConfig", "OtlpGrpc", "OtlpHttp", "Exporter", "configure", "shutdown"]


@dataclass(frozen=True, kw_only=True)
class OtelTlsConfig:
    """Native TLS certificate and private-key paths."""

    ca_certificate: str | Path | None = field(default=None, repr=False)
    client_certificate: str | Path | None = field(default=None, repr=False)
    client_private_key: str | Path | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class OtlpGrpc:
    """An explicit native OTLP gRPC destination."""

    endpoint: str = field(repr=False)
    headers: Mapping[str, str] | None = field(default=None, repr=False)
    tls: OtelTlsConfig | None = None


@dataclass(frozen=True, kw_only=True)
class OtlpHttp:
    """An explicit native OTLP HTTP destination."""

    endpoint: str = field(repr=False)
    protocol: Literal["binary", "json"] = "binary"
    headers: Mapping[str, str] | None = field(default=None, repr=False)
    tls: OtelTlsConfig | None = None


Exporter: TypeAlias = OtlpGrpc | OtlpHttp

# Immutable options from the one successful configure. Native still owns
# engine admission, subscriber conflicts and terminal failure/shutdown.
_engine_options: str | None = None


@dataclass
class _NativeConfig:
    # Keep the native wire value out of task and diagnostic representations.
    encoded: str = field(repr=False)


def _string_map(value: Mapping[str, str] | None, name: str) -> dict[str, str]:
    key: object = None
    item: object = None
    try:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping of strings to strings or None")
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise TypeError(f"{name} must be a mapping of strings to strings or None")
        return dict(value)
    finally:
        # An invalid or interrupted map can still have yielded valid credentials.
        del value, key, item


def _exporter(value: Exporter | None, signal: str) -> str | dict[str, Any]:
    if value is None:
        return "none"
    if not isinstance(value, (OtlpGrpc, OtlpHttp)):
        raise TypeError(f"{signal} must be OtlpGrpc, OtlpHttp, or None")
    if not isinstance(value.endpoint, str) or not value.endpoint:
        raise ValueError(f"{signal} endpoint must be a non-empty string")
    options: dict[str, Any] = {}
    tls: dict[str, str] = {}
    path: Any = None
    try:
        options.update(
            endpoint=value.endpoint,
            headers=_string_map(value.headers, f"{signal} headers"),
        )
        if isinstance(value, OtlpHttp):
            options["protocol"] = value.protocol
        if value.tls is not None:
            if not isinstance(value.tls, OtelTlsConfig):
                raise TypeError(f"{signal} tls must be OtelTlsConfig or None")
            for name in ("ca_certificate", "client_certificate", "client_private_key"):
                path = getattr(value.tls, name)
                if path is not None:
                    if not isinstance(path, (str, Path)):
                        raise TypeError(f"{signal} tls {name} must be a string, Path, or None")
                    # Native requires absolute paths and uses kebab-case TLS field names.
                    # Resolve cwd without putting a TLS path in pathlib's
                    # receiver if that directory is no longer available.
                    if not Path(path).is_absolute():
                        path = Path.cwd() / path
                    tls[name.replace("_", "-")] = str(Path(path))
            options["tls"] = tls
        return {"otlp-http" if isinstance(value, OtlpHttp) else "otlp-grpc": options}
    finally:
        # Release these SDK locals on success and on any later conversion error.
        del options, tls, path


async def configure(
    *,
    traces: Exporter | None = None,
    logs: Exporter | None = None,
    metrics: Exporter | None = None,
    environment: str = "dev",
    span_attributes: Mapping[str, str] | None = None,
    log_user_prompt: bool = False,
    log_agent_responses: bool = False,
    tool_result_max_bytes: int = 0,
) -> None:
    """Configure once, before opening the first Session.

    None disables that signal, including native metrics. Text switches control
    only the named native records; they do not sanitize all native telemetry.
    tool_result_max_bytes bounds result text only. A failure after native claims
    the subscriber requires a fresh process.
    """
    # Native gates response records on either OTLP kind, not its destination.
    # This marker satisfies native's required HTTP fields; only the already
    # configured process provider can route exports.
    emission = {
        "otel.exporter": (
            "none"
            if logs is None
            else {"otlp-http": {"endpoint": "http://127.0.0.1:9", "protocol": "binary"}}
        ),
        "otel.log_user_prompt": log_user_prompt,
        "otel.log_agent_responses": log_agent_responses,
        "otel.tool_result": {"max_bytes": tool_result_max_bytes},
    }
    options: dict[str, Any] = {}
    try:
        # Serialize non-secret values before building credential-bearing maps.
        snapshot = json.dumps(emission)
        options.update(
            {
                "otel.exporter": _exporter(logs, "logs"),
                "otel.trace_exporter": _exporter(traces, "traces"),
                "otel.metrics_exporter": _exporter(metrics, "metrics"),
                "analytics.enabled": metrics is not None,
                "otel.environment": environment,
                "otel.span_attributes": _string_map(span_attributes, "span_attributes"),
            }
        )
        options.update({key: item for key, item in emission.items() if key != "otel.exporter"})
        private = _NativeConfig(json.dumps(options))
    finally:
        # Release SDK config references on conversion success and failure.
        del options, span_attributes
    native = importlib.import_module("agents._native")
    from .session import _join

    async def initialize() -> None:
        global _engine_options
        message = None
        try:
            await native.configure_telemetry(private.encoded)
        except RuntimeError as error:
            # These are fixed messages from the native lifecycle owner. Other
            # errors can include caller paths, headers or endpoint credentials.
            if str(error) in (
                "Codex telemetry can only be configured once per process",
                "Configure telemetry before creating any Codex clients",
                "A Rust tracing subscriber is already installed; Codex telemetry cannot replace it",
            ):
                message = str(error)
            else:
                message = (
                    "Native telemetry configuration failed. Check the exporter settings. "
                    "If native initialization claimed the subscriber, start a new process."
                )
        finally:
            # Release the wire value after native completes, including a
            # failure joined during caller cancellation, before it propagates.
            private.encoded = ""
        if message is not None:
            raise RuntimeError(message)
        _engine_options = snapshot

    # Cancellation must join native initialization and publish a success before
    # another Session can use this process. A failure never replaces the snapshot.
    await _join(asyncio.create_task(initialize()))


async def shutdown() -> None:
    """Flush native telemetry after all Sessions close; retire it for this process.

    Requires a successful configure. It does not close Sessions or any
    application-owned Python OpenTelemetry exporters.
    """
    if _engine_options is None:
        raise RuntimeError("Configure telemetry successfully before shutdown")
    native = importlib.import_module("agents._native")
    from .session import _join

    await _join(asyncio.ensure_future(native.shutdown_telemetry()))
