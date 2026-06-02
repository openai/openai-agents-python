from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DISABLE_PROVIDER_MANAGED_RETRIES: ContextVar[bool] = ContextVar(
    "disable_provider_managed_retries",
    default=False,
)
_DISABLE_WEBSOCKET_PRE_EVENT_RETRIES: ContextVar[bool] = ContextVar(
    "disable_websocket_pre_event_retries",
    default=False,
)


@contextmanager
def provider_managed_retries_disabled(disabled: bool) -> Iterator[None]:
    token = _DISABLE_PROVIDER_MANAGED_RETRIES.set(disabled)
    try:
        yield
    finally:
        _DISABLE_PROVIDER_MANAGED_RETRIES.reset(token)


def should_disable_provider_managed_retries() -> bool:
    return _DISABLE_PROVIDER_MANAGED_RETRIES.get()


@contextmanager
def websocket_pre_event_retries_disabled(disabled: bool) -> Iterator[None]:
    token = _DISABLE_WEBSOCKET_PRE_EVENT_RETRIES.set(disabled)
    try:
        yield
    finally:
        _DISABLE_WEBSOCKET_PRE_EVENT_RETRIES.reset(token)


def should_disable_websocket_pre_event_retries() -> bool:
    return _DISABLE_WEBSOCKET_PRE_EVENT_RETRIES.get()
