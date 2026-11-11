"""Private credentials for exact immutable snapshots in active callback ownership."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any


class _TraceDeliveryOwner:
    def __init__(
        self, events: Sequence[Mapping[str, Any]], routes: Mapping[str, Any], stopped: Event
    ):
        self._stopped = stopped
        self._lock = Lock()
        self._routes = {
            id(event): (event, event["sequence"], routes[str(event["sequence"])])
            for event in events
            if event.get("credential_required") is True and str(event["sequence"]) in routes
        }

    def retire(self) -> None:
        with self._lock:
            self._routes.clear()

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
        return None


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
    invocation = route = None
    try:
        if event.get("credential_required") is not True:
            return None
        invocation = _delivery.get()
        if invocation and invocation.active and invocation.owner and id(event) in invocation.events:
            route = invocation.owner.route(event)
        if route is not None:
            return route
    finally:
        # A rejected delivery must not retain private headers in its traceback.
        del event, invocation, route
    raise RuntimeError("Native trace credential delivery is unavailable")
