from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from openai import APIStatusError


def iter_error_chain(error: Exception) -> Iterator[Exception]:
    current: Exception | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, Exception) else None


def get_status_code(error: Exception) -> int | None:
    for candidate in iter_error_chain(error):
        if isinstance(candidate, APIStatusError):
            return candidate.status_code
        for attr_name in ("status_code", "status"):
            value = getattr(candidate, attr_name, None)
            if isinstance(value, int):
                return value
    return None


def get_request_id(error: Exception) -> str | None:
    for candidate in iter_error_chain(error):
        request_id = getattr(candidate, "request_id", None)
        if isinstance(request_id, str):
            return request_id
    return None


def get_error_code(error: Exception) -> str | None:
    for candidate in iter_error_chain(error):
        error_code = getattr(candidate, "code", None)
        if isinstance(error_code, str):
            return error_code

        body = getattr(candidate, "body", None)
        if isinstance(body, Mapping):
            nested_error = body.get("error")
            if isinstance(nested_error, Mapping):
                nested_code = nested_error.get("code")
                if isinstance(nested_code, str):
                    return nested_code
            body_code = body.get("code")
            if isinstance(body_code, str):
                return body_code
    return None


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
