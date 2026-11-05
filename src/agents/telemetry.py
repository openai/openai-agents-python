"""Optional native telemetry for an SDK process.

Await configure() before opening any Sessions. Close all Sessions before
awaiting shutdown(). Native owns the exporters and the process lifetime.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Coroutine, Iterator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any, Literal, Protocol, TypeAlias

from typing_extensions import ReadOnly, TypedDict

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
    processors: Sequence[TraceProcessor] = (),
) -> None:
    """Configure once, before opening the first Session.

    None disables that signal, including native metrics. Text switches control
    only the named native records; they do not sanitize all native telemetry.
    tool_result_max_bytes bounds result text only. A failure after native claims
    the subscriber requires a fresh process.

    processors enables native trace delivery without an OTLP destination. Up
    to 16 active Python processors are supported. Configure before any Session;
    later add/set calls change routes without replacing the native provider.
    """
    processors = tuple(processors)
    _validate_processors(processors)
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
        if processors:
            options["otel.trace_processors"] = True
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
        if processors:
            _set_processors(processors, replace=False)

    # Cancellation must join native initialization and publish a success before
    # another Session can use this process. A failure never replaces the snapshot.
    await _join(asyncio.create_task(initialize()))


async def shutdown(*, timeout_ms: int | None = None) -> None:
    """Flush native telemetry after all Sessions close; retire it for this process.

    Requires a successful configure. It does not close Sessions or any
    application-owned Python OpenTelemetry exporters.
    """
    if _engine_options is None:
        raise RuntimeError("Configure telemetry successfully before shutdown")
    native = importlib.import_module("agents._native")
    from .session import _join

    budget = None if timeout_ms is None else _timeout(timeout_ms)

    async def retire() -> None:
        global _processor_task
        try:
            _ensure_processor_pump()
            if budget is None:
                await native.shutdown_telemetry()
            else:
                await native.shutdown_telemetry(budget)
        finally:
            processor_task = _processor_task
            if processor_task is not None and not processor_task.done():
                processor_task.cancel()
                await asyncio.gather(processor_task, return_exceptions=True)
            _processor_task = None
            for processor in _processors.values():
                processor.close()
            _processors.clear()
            _active_processor_ids.clear()
            _retired_routes.clear()

    # Own native retirement and Python cleanup together, including a failure
    # or repeated cancellation of the caller waiting for terminal shutdown.
    await _join(asyncio.create_task(retire()))


@dataclass(frozen=True, kw_only=True)
class TraceExportCredentials:
    """Private per-trace header overrides, copied at construction.

    Native validates the HTTP/gRPC headers before a span is admitted.
    Credentials never enter lifecycle records or SessionState.
    """

    headers: Mapping[str, str] = field(repr=False)

    def __init__(self, *, headers: Mapping[str, str]) -> None:
        from types import MappingProxyType

        try:
            object.__setattr__(self, "headers", MappingProxyType(_string_map(headers, "headers")))
        finally:
            # Rejected mappings must not survive in constructor traceback frames.
            del headers, self


@dataclass(frozen=True, kw_only=True)
class SpanContext:
    """Immutable native identity; the private handle is process-local."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    disabled: bool
    include_sensitive_data: bool
    _handle: str = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class TraceOptions:
    """Optional native operation policy. Omitted child policy inherits.

    A new root defaults to metadata-only. An ancestor's disabled or
    metadata-only policy cannot be relaxed. Detached clears linkage only.
    """

    parent: SpanContext | None = None
    detached: bool = False
    trace_id: str | None = None
    group_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    disabled: bool | None = None
    include_sensitive_data: bool | None = None
    export_credentials: TraceExportCredentials | None = field(default=None, repr=False)


_current: ContextVar[SpanContext | None] = ContextVar("agents_trace_context", default=None)


def current_context() -> SpanContext | None:
    """Return the active SDK scope, independently in each task or callback."""
    return _current.get()


def _native_tracing() -> Any:
    return importlib.import_module("agents._native")


def _options(value: TraceOptions | Mapping[str, Any] | None) -> TraceOptions:
    if value is None:
        return TraceOptions()
    if isinstance(value, Mapping):
        try:
            value = TraceOptions(**value)
        except (TypeError, ValueError):
            raise ValueError("Invalid trace options") from None
    if not isinstance(value, TraceOptions):
        raise TypeError("trace_options must be TraceOptions or a mapping")
    if value.parent is not None and not isinstance(value.parent, SpanContext):
        raise TypeError("parent must be SpanContext or None")
    for flag in (value.disabled, value.include_sensitive_data):
        if flag is not None and not isinstance(flag, bool):
            raise TypeError("trace policy flags must be bool or None")
    if not isinstance(value.detached, bool):
        raise TypeError("detached must be bool")
    if value.export_credentials is not None and not isinstance(
        value.export_credentials, TraceExportCredentials
    ):
        raise TypeError("export_credentials must be TraceExportCredentials")
    return value


class TraceScope:
    """A synchronous native span scope; ending restores context immediately.

    Admitted native work retains its own context after scope exit. Do not
    persist or send SpanContext values to another process.
    """

    def __init__(
        self,
        name: str,
        kind: str,
        options: TraceOptions | Mapping[str, Any] | None,
        *,
        _restrictions: tuple[str, ...] = (),
    ):
        self._name = name
        self._restrictions = _restrictions
        self._kind = kind
        self._options = _options(options)
        self._context: SpanContext | None = None
        self._token: Token[SpanContext | None] | None = None
        self._ended = False
        self._outcome: dict[str, Any] | None = None

    @property
    def context(self) -> SpanContext:
        if self._context is None:
            raise RuntimeError("Enter the trace scope before reading its context")
        return self._context

    def start(self) -> TraceScope:
        """Start and activate this span in the current task.

        Call end() in the same task to restore its previous context.
        Use options.detached to start without a parent link.
        """
        return self.__enter__()

    def end(self) -> None:
        """End once and restore context in the task that called start()."""
        self.__exit__(None, None, None)

    def __enter__(self) -> TraceScope:
        if self._context is not None or self._ended:
            raise RuntimeError("A trace scope can only be entered once")
        value = self._options
        ambient = current_context()
        parent = value.parent or ambient
        native = _native_tracing()
        credential = None
        try:
            # Validate public JSON before registering any credential or span.
            wire: dict[str, Any] = {
                "name": self._name,
                "kind": self._kind,
                "parent": parent._handle if parent else None,
                "restrictions_parent": (
                    ambient._handle if ambient is not None and ambient is not parent else None
                ),
                "detached": value.detached,
                "trace_id": value.trace_id,
                "group_id": value.group_id,
                "metadata": dict(value.metadata),
                "disabled": value.disabled,
                "include_sensitive_data": value.include_sensitive_data,
                "credential_ref": None,
            }
            if self._restrictions:
                wire["restrictions_parents"] = list(self._restrictions)
            json.dumps(wire, allow_nan=False)
            if value.export_credentials is not None:
                credential = native.register_trace_credentials(
                    json.dumps(dict(value.export_credentials.headers))
                )
                wire["credential_ref"] = credential
            data = json.loads(native.start_trace_span(json.dumps(wire, allow_nan=False)))
            handle = data.pop("handle")
            self._context = SpanContext(_handle=handle, **data)
        finally:
            if credential is not None:
                native.release_trace_credentials(credential)
        self._token = _current.set(self._context)
        return self

    def set_outcome(self, *, status: str, category: str | None = None) -> None:
        if status not in ("ok", "error", "cancelled"):
            raise ValueError("Invalid span outcome status")
        self._outcome = {"status": status, "category": category, "data": {}}

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._ended or self._context is None:
            return
        if self._token is not None:
            _current.reset(self._token)
            self._token = None
        self._ended = True
        status = (
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "error" if exc else "ok"
        )
        outcome = self._outcome or {
            "status": status,
            "category": status if exc else None,
            "data": {},
        }
        native = _native_tracing()
        try:
            native.end_trace_span(self._context._handle, json.dumps(outcome))
        finally:
            native.release_trace_span(self._context._handle)


def trace(name: str, *, options: TraceOptions | Mapping[str, Any] | None = None) -> TraceScope:
    """Start a workflow scope on entry, metadata-only unless explicitly enabled."""
    return TraceScope(name, "workflow", options)


def span(name: str, *, options: TraceOptions | Mapping[str, Any] | None = None) -> TraceScope:
    """Start a custom native span on entry, inheriting active restrictions."""
    return TraceScope(name, "custom", options)


class _CapturedTrace:
    """Host lease plus optional operation span; never part of saved state."""

    def __init__(self, context: str | None, scope: TraceScope | None):
        self.context = context
        self.scope = scope

    def close(
        self,
        error: BaseException | None = None,
        *,
        cancelled: bool = False,
        category: str | None = None,
    ) -> None:
        context, self.context = self.context, None
        scope, self.scope = self.scope, None
        try:
            if context is not None:
                _native_tracing().release_trace_context(context)
        finally:
            if scope is not None:
                # Scope is no longer ambient; capture restores it synchronously.
                if cancelled:
                    scope.set_outcome(status="cancelled", category="cancelled")
                elif category is not None:
                    scope.set_outcome(status="error", category=category)
                scope.__exit__(type(error) if error else None, error, None)


def _capture(
    options: TraceOptions | Mapping[str, Any] | None = None,
    fallback: str | None = None,
) -> _CapturedTrace:
    _ensure_processor_pump()
    scope = None
    native = None
    context = current_context()
    handle = context._handle if context else fallback
    if options is not None or (context is not None and fallback is not None):
        # Native operation spans own their immutable parent and policy. The
        # caller's ambient context is restored before any queue or await.
        scope = TraceScope(
            "operation",
            "operation",
            options,
            _restrictions=(fallback,) if fallback is not None else (),
        )
        if context is None and fallback is not None:
            # Session defaults contribute policy even when no public scope is active.
            native = _native_tracing()
            data = json.loads(native.trace_span_context(fallback))
            data.pop("handle", None)
            token = _current.set(SpanContext(_handle=fallback, **data))
            try:
                scope.__enter__()
                assert scope._token is not None
                _current.reset(scope._token)
                scope._token = None
            finally:
                _current.reset(token)
        else:
            scope.__enter__()
        if scope._token is not None:
            _current.reset(scope._token)
            scope._token = None
        handle = scope.context._handle
    try:
        return _CapturedTrace(
            (native or _native_tracing()).retain_trace_context(handle) if handle else None, scope
        )
    except BaseException as error:
        if scope is not None:
            scope.__exit__(type(error), error, None)
        raise


class TraceLifecycleEvent(TypedDict):
    """An immutable native span snapshot delivered to a trace processor.

    dropped_attributes_count is a nonnegative native count of attributes lost
    before this snapshot. It remains observable even when delivery succeeds.
    Nanosecond timestamps use decimal strings to preserve their exact values.
    """

    sequence: ReadOnly[int]
    processor_ids: ReadOnly[Sequence[str]]
    lifecycle: ReadOnly[Literal["start", "end"]]
    trace_id: ReadOnly[str]
    span_id: ReadOnly[str]
    parent_span_id: ReadOnly[str | None]
    name: ReadOnly[str]
    kind: ReadOnly[str]
    start_time_unix_nanos: ReadOnly[str]
    end_time_unix_nanos: ReadOnly[str | None]
    attributes: ReadOnly[Mapping[str, Any]]
    dropped_attributes_count: ReadOnly[int]
    outcome: ReadOnly[Literal["ok", "error", "unset"]]


class TraceProcessor(Protocol):
    """Receive immutable native lifecycle snapshots; callbacks may be async.

    This native interface is separate from classic v0 processors. Each event
    runs on a private worker loop and has a one-second delivery budget. At most
    16 workers may remain live, including stalled removed routes. A stalled
    route or exhausted worker capacity loses further events. Flush reports
    those failures without changing runs. Callbacks must cooperate to release
    their worker; the SDK bounds its wait and cannot preempt user code.
    """

    def on_trace_event(self, event: TraceLifecycleEvent) -> Any: ...


@dataclass(frozen=True, kw_only=True)
class FlushResult:
    status: Literal["success", "partial", "timeout", "failure"]
    dropped_records: int
    failed_deliveries: int
    barrier: int


_processors: dict[str, _ProcessorRoute] = {}
_processor_task: asyncio.Task[None] | None = None
_processor_serial = 0
_active_processor_ids: set[str] = set()
_retired_routes: dict[str, int] = {}
_processor_acknowledged = 0
_worker_count = 0
_worker_lock = Lock()
_MAX_PROCESSOR_WORKERS = 16


def _validate_processors(processors: Sequence[TraceProcessor]) -> None:
    if len(processors) > _MAX_PROCESSOR_WORKERS:
        raise RuntimeError("At most 16 active Python trace processors are supported")
    if any(not callable(getattr(processor, "on_trace_event", None)) for processor in processors):
        raise TypeError("Processors must define on_trace_event")


def _ensure_processor_pump() -> None:
    global _processor_task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _processors and (
        _processor_task is None or _processor_task.done() or _processor_task.get_loop().is_closed()
    ):
        _processor_task = asyncio.create_task(_pump_processors())


def _set_processors(processors: Sequence[TraceProcessor], *, replace: bool) -> None:
    global _processor_serial, _processor_task, _active_processor_ids
    values = tuple(processors)
    _validate_processors(values)
    active_count = len(values) if replace else len(_active_processor_ids) + len(values)
    if active_count > _MAX_PROCESSOR_WORKERS:
        raise RuntimeError("At most 16 active Python trace processors are supported")
    if len(_processors) + len(values) > 64:
        raise RuntimeError(
            "Trace processor route capacity exhausted; flush completed records before adding routes"
        )
    ids = [f"python-{_processor_serial + i}" for i in range(len(values))]
    native = _native_tracing()
    cutoff = native.set_trace_routes(json.dumps({"processor_ids": ids, "replace": replace}))
    if replace:
        _retired_routes.update((identifier, cutoff) for identifier in _active_processor_ids)
        _active_processor_ids = set(ids)
    else:
        _active_processor_ids.update(ids)
    _processor_serial += len(values)
    _processors.update(
        (identifier, _ProcessorRoute(value)) for identifier, value in zip(ids, values, strict=True)
    )
    _retire_routes(_processor_acknowledged)
    _ensure_processor_pump()


def _retire_routes(through_sequence: int) -> None:
    for identifier, cutoff in tuple(_retired_routes.items()):
        if cutoff <= through_sequence:
            route = _processors.pop(identifier, None)
            if route is not None:
                route.close()
            del _retired_routes[identifier]


def add_trace_processor(processor: TraceProcessor) -> None:
    """Add a route after configure; preserve the selected native exporter."""
    _set_processors([processor], replace=False)


def set_trace_processors(processors: Sequence[TraceProcessor]) -> None:
    """Replace all future trace routes, including the native exporter.

    Removed routes remain alive to deliver already queued records. A removed
    callback can retain worker capacity until it returns. Delivery waits stay
    bounded; exhausted worker capacity counts as failed delivery. At most 64
    current and retired route descriptors may be retained; flush before adding
    more routes when that bound is reached.
    """
    _set_processors(processors, replace=True)


@dataclass
class _ProcessorDelivery:
    event: TraceLifecycleEvent
    result: Future[tuple[bool, float]] = field(default_factory=Future)


@dataclass
class _ProcessorBatch:
    deliveries: tuple[_ProcessorDelivery, ...]
    admitted_at: float = field(default_factory=monotonic)
    stopped: Event = field(default_factory=Event)
    completion: Future[None] | None = None

    def done(self) -> bool:
        # The worker completes records in order, including fenced records.
        return self.deliveries[-1].result.done()

    async def wait(self) -> int:
        failures = 0
        previous_finished = self.admitted_at
        try:
            for index, delivery in enumerate(self.deliveries):
                if delivery.result.done():
                    success, previous_finished = delivery.result.result()
                else:
                    remaining = max(0.0, previous_finished + 1.0 - monotonic())
                    completion = asyncio.wrap_future(delivery.result)
                    done, _ = await asyncio.wait((completion,), timeout=remaining)
                    if not done:
                        return failures + len(self.deliveries) - index
                    success, previous_finished = completion.result()
                failures += not success
            return failures
        finally:
            # A late callback may finish, but cannot begin subsequent records
            # after the host times out, is cancelled, or retires this batch.
            self.stopped.set()


class _ProcessorRoute:
    """One private daemon loop and one bounded batch in flight per route."""

    def __init__(self, processor: TraceProcessor):
        self.processor = processor
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending: _ProcessorBatch | None = None

    async def _invoke(self, event: TraceLifecycleEvent) -> None:
        import inspect

        failed = False
        try:
            result = self.processor.on_trace_event(event)
            if inspect.isawaitable(result):
                await result
        except BaseException:
            # Contain even callback cancellation and BaseException subclasses.
            # Raise outside the handler so raw callback errors are not chained.
            failed = True
        if failed:
            raise RuntimeError("Trace processor delivery failed")

    async def _invoke_batch(self, batch: _ProcessorBatch) -> None:
        previous_finished = batch.admitted_at
        for delivery in batch.deliveries:
            now = monotonic()
            deadline = previous_finished + 1.0
            if batch.stopped.is_set() or now >= deadline:
                batch.stopped.set()
                delivery.result.set_result((False, now))
                continue
            success = True
            try:
                await self._invoke(delivery.event)
            except Exception:
                success = False
            finished = monotonic()
            if finished >= deadline:
                # Check on the worker too: a delayed host timer must not let a
                # late-returning callback start the remainder of this group.
                batch.stopped.set()
                success = False
            delivery.result.set_result((success, finished))
            previous_finished = finished

    def submit(self, events: Sequence[TraceLifecycleEvent]) -> _ProcessorBatch:
        from threading import Thread

        global _worker_count
        if self.loop is None:
            with _worker_lock:
                if _worker_count >= _MAX_PROCESSOR_WORKERS:
                    raise RuntimeError("Trace processor worker capacity exhausted")
                _worker_count += 1
            try:
                loop = asyncio.new_event_loop()
            except BaseException:
                with _worker_lock:
                    _worker_count -= 1
                raise
            self.loop = loop

            def serve() -> None:
                global _worker_count
                asyncio.set_event_loop(loop)
                try:
                    loop.run_forever()
                finally:
                    loop.close()
                    with _worker_lock:
                        _worker_count -= 1

            try:
                Thread(target=serve, daemon=True, name="agents-trace-processor").start()
            except BaseException:
                loop.close()
                self.loop = None
                with _worker_lock:
                    _worker_count -= 1
                raise
        batch = _ProcessorBatch(tuple(_ProcessorDelivery(event) for event in events))
        work = self._invoke_batch(batch)
        try:
            batch.completion = asyncio.run_coroutine_threadsafe(work, self.loop)
        except BaseException:
            work.close()
            raise
        self.pending = batch
        return batch

    def close(self) -> None:
        loop = self.loop
        if loop is None:
            return

        def stop(_future: Any = None) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)

        if self.pending is not None:
            self.pending.stopped.set()
            completion = self.pending.completion
            if completion is not None and not completion.done():
                # A blocked callback retains only its bounded batch and worker.
                completion.add_done_callback(stop)
                return
        stop()


def _freeze(value: Any) -> Any:
    from types import MappingProxyType

    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class _ProcessorDrain(asyncio.Task[None]):
    """Join one bounded native read before its irreversible result is released."""

    def __init__(self, work: Coroutine[Any, Any, str]):
        self.outcome: Future[str] = Future()

        async def collect() -> None:
            try:
                self.outcome.set_result(await work)
            except BaseException as error:
                self.outcome.set_exception(error)

        super().__init__(collect())

    def cancel(self, msg: object | None = None) -> bool:
        # asyncio.run cancels every Task, including shielded children. Native
        # cancellation discards this destructive read's result; retain it until
        # the pump can account for the selected deliveries instead.
        return False


async def _pump_processors() -> None:
    from .session import _join

    global _processor_acknowledged
    native = _native_tracing()
    while True:
        drain = _ProcessorDrain(native.drain_trace_events(64))
        deliveries: list[_ProcessorBatch] = []
        batch: Any = None
        try:
            await asyncio.shield(drain)
            batch = json.loads(drain.outcome.result())
            routes: dict[str, list[TraceLifecycleEvent]] = {}
            for event in batch["events"]:
                snapshot = _freeze(event)
                for identifier in event.get("processor_ids", []):
                    routes.setdefault(identifier, []).append(snapshot)
            for identifier, events in routes.items():
                processor = _processors.get(identifier)
                if processor is None or (
                    processor.pending is not None and not processor.pending.done()
                ):
                    continue
                try:
                    delivery = processor.submit(events)
                except Exception:
                    continue
                deliveries.append(delivery)
                await delivery.wait()
        finally:
            # Retain the result even if loop shutdown cancels the pump before
            # its native read finishes or before Python resumes with that read.
            try:
                await _join(drain)
            except asyncio.CancelledError:
                pass
            if batch is None:
                batch = json.loads(drain.outcome.result())
            for delivery in deliveries:
                delivery.stopped.set()
            selected = sum(len(event.get("processor_ids", [])) for event in batch["events"])
            completed = sum(
                record.result.done() and record.result.result()[0]
                for delivery in deliveries
                for record in delivery.deliveries
            )
            if batch["through_sequence"]:
                native.ack_trace_events(batch["through_sequence"], selected - completed)
                _processor_acknowledged = max(_processor_acknowledged, batch["through_sequence"])
                _retire_routes(_processor_acknowledged)
        if not batch["events"]:
            await asyncio.sleep(0.01)


def _timeout(timeout_ms: int) -> int:
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or not 0 < timeout_ms <= 60_000
    ):
        raise ValueError("timeout_ms must be an integer from 1 through 60000")
    return timeout_ms


async def force_flush(*, timeout_ms: int = 10_000) -> FlushResult:
    """Flush completed records through a barrier within one total budget.

    Success means exporter calls completed without known loss, not storage or
    viewer visibility. Unfinished spans and future records are excluded.
    """
    from .session import _join

    if _engine_options is None:
        raise RuntimeError("Configure telemetry successfully before force_flush")
    budget = _timeout(timeout_ms)
    _ensure_processor_pump()
    result = await _join(asyncio.ensure_future(_native_tracing().force_flush_telemetry(budget)))
    return FlushResult(**json.loads(result))


__all__ += [
    "TraceExportCredentials",
    "TraceOptions",
    "SpanContext",
    "TraceScope",
    "TraceProcessor",
    "TraceLifecycleEvent",
    "FlushResult",
    "trace",
    "span",
    "current_context",
    "add_trace_processor",
    "set_trace_processors",
    "force_flush",
]


@contextmanager
def _activate(context: str | None) -> Iterator[None]:
    """Borrow a native callback/submission owner for this invocation only."""
    if context is None:
        yield
        return
    data = json.loads(_native_tracing().trace_span_context(context))
    data.pop("handle", None)
    token = _current.set(SpanContext(_handle=context, **data))
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def _owned_span(name: str, kind: str, context: str | None = None) -> Iterator[TraceScope | None]:
    with _activate(context):
        if current_context() is None:
            yield None
        else:
            with TraceScope(name, kind, None) as active:
                try:
                    yield active
                except BaseException as error:
                    active.set_outcome(
                        status="cancelled"
                        if isinstance(error, asyncio.CancelledError)
                        else "error",
                        category="cancelled"
                        if isinstance(error, asyncio.CancelledError)
                        else "validation_error"
                        if kind == "validation"
                        else "callback_error",
                    )
                    raise
