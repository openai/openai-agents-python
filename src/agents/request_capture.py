"""Local observations of Runner model submissions and provider transports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from openai import AsyncOpenAI

Owner = Literal["root", "child"]
Operation = Literal["model", "compaction"]
Route = Literal["http", "websocket", "borrowed_http", "native", "custom"]
Coverage = Literal["complete", "partial", "unknown"]


@dataclass(frozen=True)
class TransportCounts:
    """Local header or frame write starts, completed writes and first replies."""

    attempts: int
    writes: int
    replies: int


@dataclass(frozen=True)
class RequestRoute:
    """Observations for one owner, operation and transport. None means unavailable."""

    owner: Owner
    operation: Operation
    route: Route
    submissions: int
    coverage: Coverage
    observed: TransportCounts | None


@dataclass(frozen=True)
class RequestTotals:
    """Immutable snapshot. Partial routes retain only known observations."""

    routes: tuple[RequestRoute, ...]
    coverage: Coverage

    @property
    def model_submissions(self) -> int:
        """Regular Python model submissions, including attached children."""
        return sum(row.submissions for row in self.routes if row.operation == "model")

    @property
    def observed(self) -> TransportCounts | None:
        """Known local transport observations across routes. Check coverage for gaps."""
        if self.coverage == "unknown":
            return None
        counts = [row.observed for row in self.routes if row.observed is not None]
        return TransportCounts(
            sum(c.attempts for c in counts),
            sum(c.writes for c in counts),
            sum(c.replies for c in counts),
        )

    @property
    def compactions(self) -> int:
        """Separate automatic session compaction submissions."""
        return sum(row.submissions for row in self.routes if row.operation == "compaction")


@dataclass
class _Counts:
    submissions: int = 0
    attempts: int = 0
    writes: int = 0
    replies: int = 0
    gap: bool = False


@dataclass
class _Scope:
    rows: dict[tuple[Owner, Operation, Route], _Counts] = field(default_factory=dict)
    claimed: bool = False
    finished: bool = False
    closed: bool = False

    def snapshot(self) -> RequestTotals:
        rows = []
        for (owner, operation, route), counts in sorted(self.rows.items()):
            observed = None
            coverage: Coverage = "unknown"
            if route in ("http", "websocket") and (
                not counts.gap or counts.attempts or counts.writes or counts.replies
            ):
                observed = TransportCounts(counts.attempts, counts.writes, counts.replies)
                coverage = "partial" if counts.gap or not self.finished else "complete"
            rows.append(
                RequestRoute(owner, operation, route, counts.submissions, coverage, observed)
            )
        statuses = {row.coverage for row in rows}
        if not self.claimed or statuses == {"unknown"}:
            coverage = "unknown"
        elif not self.finished or statuses - {"complete"}:
            coverage = "partial"
        else:
            coverage = "complete"
        return RequestTotals(tuple(rows), coverage)


@dataclass(frozen=True)
class _Run:
    scope: _Scope
    owner: Owner

    def finish(self, _task: object = None) -> None:
        if self.owner == "root":
            self.scope.finished = True

    def submit(self, operation: Operation, route: Route, *, first: bool = True) -> _Counts:
        counts = self.scope.rows.setdefault((self.owner, operation, route), _Counts())
        if first:
            counts.submissions += 1
        return counts


_next: ContextVar[_Run | None] = ContextVar("next_request_run", default=None)
_run: ContextVar[_Run | None] = ContextVar("request_run", default=None)
_model: ContextVar[tuple[int, _Counts] | None] = ContextVar("request_model", default=None)
_http: ContextVar[_Counts | None] = ContextVar("request_http", default=None)


class RequestCapture:
    """Observe the first Python Runner invocation in a with block.

    Example::

        capture = RequestCapture()
        try:
            with capture:
                result = await Runner.run(agent, prompt)
        finally:
            print(capture.totals.coverage, capture.totals.observed)

    Read totals after success, cancellation or failure. Drain streams inside the
    block. Early exit freezes a partial snapshot. Reuse starts a fresh capture,
    including on approval resume. Handoffs, corrections and follow-ups belong
    to the root. Agent tools count as children. Independent runs are excluded.

    One regular submission includes Runner and client retries. Automatic session
    compaction is separate. HTTP and SSE count local header write attempts,
    completed header writes and received response headers, including redirects
    and errors. WebSocket counts awaited request frame sends, successful send
    returns and the first reply frame. Handshakes and proxy CONNECT are excluded.
    An attempt can fail before any bytes leave the process.

    Coverage refers only to these local boundaries. HTTP/2 is partial. Native,
    hosted and custom models and borrowed HTTP transports have unknown counts.
    Partial coverage retains known observations. No count establishes delivery,
    model success or billing. Unused captures have unknown coverage.
    """

    def __init__(self) -> None:
        self._scope: _Scope | None = None
        self._saved = RequestTotals((), "unknown")

    @property
    def totals(self) -> RequestTotals:
        return self._scope.snapshot() if self._scope is not None else self._saved

    def __enter__(self) -> RequestCapture:
        if self._scope is not None:
            raise RuntimeError("RequestCapture is already active.")
        self._scope = _Scope()
        self._token = _next.set(_Run(self._scope, "root"))
        return self

    def __exit__(self, *_: object) -> None:
        _next.reset(self._token)
        del self._token
        assert self._scope is not None
        self._scope.closed = True
        self._saved = self._scope.snapshot()
        self._scope = None


def _claim_run() -> _Run | None:
    run = _next.get()
    if run is None or run.scope.closed or run.scope.finished:
        return None
    if run.owner == "root":
        if run.scope.claimed:
            return None
        run.scope.claimed = True
    return run


@contextmanager
def _capture_run(run: _Run | None, *, streamed: bool = False) -> Iterator[None]:
    if run is None and _run.get() is None:
        yield
        return
    parent = _run.set(run)
    pending = _next.set(None)
    model = _model.set(None)
    http = _http.set(None)
    try:
        yield
        if streamed:
            run = None  # The producer inherits this context and owns completion.
    finally:
        if run is not None:
            run.finish()
        _http.reset(http)
        _model.reset(model)
        _next.reset(pending)
        _run.reset(parent)


@contextmanager
def _attached_run() -> Iterator[None]:
    run = _run.get()
    if run is None or _next.get() is not None:
        yield
        return
    token = _next.set(_Run(run.scope, "child"))
    try:
        yield
    finally:
        _next.reset(token)


def _http_route(client: object) -> Route:
    http = getattr(client, "_client", None)
    if (
        isinstance(client, _ObservedOpenAI)
        and isinstance(http, httpx2.AsyncClient)
        and _on_request in http.event_hooks["request"]
    ):
        return "http"
    return "borrowed_http"


def _route(model: object) -> Route:
    from .models.codex import _CodexModel
    from .models.openai_chatcompletions import OpenAIChatCompletionsModel
    from .models.openai_responses import OpenAIResponsesModel, OpenAIResponsesWSModel

    if type(model) is OpenAIResponsesWSModel:
        return "websocket"
    if type(model) in (OpenAIResponsesModel, OpenAIChatCompletionsModel):
        return _http_route(getattr(model, "_client", None))
    if isinstance(model, _CodexModel):
        return "native"
    return "custom"


@contextmanager
def _model_submission(model: object, *, first: bool = True) -> Iterator[None]:
    run = _run.get()
    if model is None or run is None or run.scope.closed:
        yield
        return
    route = _route(model)
    counts = run.submit("model", route, first=first)
    token = _model.set((id(model), counts) if route in ("http", "websocket") else None)
    try:
        yield
    finally:
        _model.reset(token)


def _model_counts(model: object) -> _Counts | None:
    active = _model.get()
    return active[1] if active is not None and active[0] == id(model) else None


def _compact_submission(client: object) -> _Counts | None:
    run = _run.get()
    if run is None or run.scope.closed:
        return None
    route = _http_route(client)
    counts = run.submit("compaction", route)
    return counts if route == "http" else None


@contextmanager
def _http_exchange(counts: _Counts | None) -> Iterator[None]:
    if counts is None and _http.get() is None:
        yield
        return
    token = _http.set(counts)
    try:
        yield
    finally:
        _http.reset(token)


class _Trace:
    """One HTTP hop. Never retain the request, trace payload or run."""

    def __init__(self, previous: Callable[[str, dict[str, Any]], Awaitable[None]] | None) -> None:
        self.previous = previous
        self.connect = False
        self.replied = False

    async def __call__(self, name: str, info: dict[str, Any]) -> None:
        send = _send.get()
        if send is not None and send.trace is self:
            self._record(send.counts, name, info)
        if self.previous is not None:
            await self.previous(name, info)

    def _record(self, counts: _Counts, name: str, info: dict[str, Any]) -> None:
        if name in ("http11.send_request_headers.started", "http2.send_request_headers.started"):
            method = getattr(info.get("request"), "method", None)
            if method is None:
                counts.gap = True
            self.connect = method in (None, b"CONNECT", "CONNECT")
            if not self.connect:
                counts.attempts += 1
                if name.startswith("http2."):
                    counts.gap = True
        elif not self.connect:
            if name in (
                "http11.send_request_headers.complete",
                "http2.send_request_headers.complete",
            ):
                counts.writes += 1
            elif name in (
                "http11.receive_response_headers.complete",
                "http2.receive_response_headers.complete",
            ):
                counts.replies += 1
                self.replied = True


@dataclass
class _Send:
    counts: _Counts
    marker: object = field(default_factory=object)
    trace: _Trace | None = None
    unmarked: bool = False


_send: ContextVar[_Send | None] = ContextVar("provider_send", default=None)
_MARKER = "agents.request_capture"


class _ObservedOpenAI(AsyncOpenAI):
    """Bind each client attempt before auth and HTTP hooks can issue other requests."""

    @property
    def user_agent(self) -> str:
        return super().user_agent.replace(type(self).__name__, AsyncOpenAI.__name__, 1)

    async def _send_request(
        self, request: httpx2.Request, *, stream: bool, **kwargs: Any
    ) -> httpx2.Response:
        counts = _http.get()
        if counts is None:
            return await super()._send_request(request, stream=stream, **kwargs)

        send = _Send(counts)
        request.extensions[_MARKER] = send.marker
        token = _send.set(send)
        try:
            with _http_exchange(None):
                response = await super()._send_request(request, stream=stream, **kwargs)
            # Auth can discard earlier redirects from history. Check ownership of
            # each retained response, and treat unmarked multi-hop traffic as a gap.
            if (
                any(
                    hop.request.extensions.get(_MARKER) is not send.marker
                    for hop in (*response.history, response)
                )
                or (send.unmarked and bool(response.history))
                or send.trace is None
                or not send.trace.replied
                or response.request.extensions.get("trace") is not send.trace
            ):
                counts.gap = True
            return response
        except BaseException:
            # No response hook runs on cancellation or disconnect. A trace lost
            # before the first write is indistinguishable from an invisible send.
            if (
                send.unmarked
                or send.trace is None
                or send.trace.replied
                or request.extensions.get("trace") is not send.trace
            ):
                counts.gap = True
            raise
        finally:
            send.trace = None
            _send.reset(token)
            request.extensions.pop(_MARKER, None)


async def _on_request(request: httpx2.Request) -> None:
    send = _send.get()
    if send is None:
        return
    if request.extensions.get(_MARKER) is not send.marker:
        # Without a final history, a fresh auth request and hook traffic look alike.
        send.unmarked = True
        return
    if send.trace is not None and not send.trace.replied:
        send.counts.gap = True
    trace = request.extensions.get("trace")
    # Redirects copy extensions and challenges can reuse the request.
    send.trace = _Trace(trace.previous if isinstance(trace, _Trace) else trace)
    request.extensions["trace"] = send.trace


def _instrument_http(client: object) -> None:
    if not isinstance(client, httpx2.AsyncClient):
        return
    client.event_hooks["request"].append(_on_request)


async def _send_frame(counts: _Counts, connection: Any, frame: str) -> None:
    counts.attempts += 1
    await connection.send(frame)
    counts.writes += 1
