"""Private credentials for exact immutable snapshots in active callback ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any

_UNAVAILABLE_ROUTE = object()


class _TraceDeliveryOwner:
    def __init__(
        self,
        events: Sequence[Mapping[str, Any]],
        routes: Mapping[str, Any],
        stopped: Event,
        processor_id: str,
    ):
        self._stopped = stopped
        self._lock = Lock()
        self._export_tasks: set[asyncio.Task[Any]] = set()
        self._routes = {
            id(event): (
                event,
                event["sequence"],
                routes[str(event["sequence"])]
                if processor_id in routes[str(event["sequence"])]["processor_ids"]
                else None,
            )
            for event in events
            if event.get("credential_required") is True and str(event["sequence"]) in routes
        }

    def retire(self) -> None:
        with self._lock:
            self._stopped.set()
            self._routes.clear()
            for task in self._export_tasks:
                task.get_loop().call_soon_threadsafe(task.cancel)
            self._export_tasks.clear()

    @contextmanager
    def export_scope(self) -> Iterator[None]:
        task = asyncio.current_task()
        assert task is not None
        with self._lock:
            if self._stopped.is_set():
                raise asyncio.CancelledError
            self._export_tasks.add(task)
        try:
            yield
        finally:
            with self._lock:
                self._export_tasks.discard(task)

    def route(self, event: Mapping[str, Any]) -> Any:
        with self._lock:
            entry = self._routes.get(id(event))
            if (
                not self._stopped.is_set()
                and entry
                and entry[0] is event
                and entry[1] == event["sequence"]
            ):
                return entry[2]
        return _UNAVAILABLE_ROUTE


@dataclass
class _Invocation:
    owner: _TraceDeliveryOwner | None
    events: frozenset[int]
    active: bool = True


_delivery: ContextVar[_Invocation | None] = ContextVar("agents_trace_delivery", default=None)


@contextmanager
def _trace_delivery_context(
    owner: _TraceDeliveryOwner | None, events: Sequence[Mapping[str, Any]]
) -> Iterator[None]:
    invocation = _Invocation(owner, frozenset(id(event) for event in events))
    token = _delivery.set(invocation)
    try:
        yield
    finally:
        invocation.active = False
        _delivery.reset(token)


def _trace_credential_route(event: Mapping[str, Any]) -> Any:
    invocation = None
    route = _UNAVAILABLE_ROUTE
    try:
        invocation = _delivery.get()
        if event.get("credential_required") is not True:
            if invocation and (
                not invocation.active or (invocation.owner and invocation.owner._stopped.is_set())
            ):
                raise asyncio.CancelledError
            return None
        if invocation and invocation.active and invocation.owner and id(event) in invocation.events:
            route = invocation.owner.route(event)
        if route is not _UNAVAILABLE_ROUTE:
            return route
    finally:
        # A rejected delivery must not retain private headers in its traceback.
        del event, invocation, route
    raise RuntimeError("Native trace credential delivery is unavailable")


@asynccontextmanager
async def _trace_export_cancellation() -> AsyncIterator[None]:
    """Cancel only built-in HTTP work when its callback owner retires."""
    invocation = _delivery.get()
    if invocation is None or invocation.owner is None:
        yield
        return
    if not invocation.active:
        raise asyncio.CancelledError
    with invocation.owner.export_scope():
        yield
