from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from openai import APIStatusError


def iter_error_chain(error: Exception) -> Iterator[Exception]:
    current: Exception | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, Exception) else None


def header_lookup(headers: Any, key: str) -> str | None:
    normalized_key = key.lower()
    if isinstance(headers, httpx.Headers):
        value = headers.get(key)
        return value if isinstance(value, str) else None
    if isinstance(headers, Mapping):
        for header_name, header_value in headers.items():
            if str(header_name).lower() == normalized_key and isinstance(header_value, str):
                return header_value
    return None


def _get_candidate_header(candidate: Exception, key: str) -> str | None:
    response = getattr(candidate, "response", None)
    if isinstance(response, httpx.Response):
        header_value = header_lookup(response.headers, key)
        if header_value is not None:
            return header_value

    for attr_name in ("headers", "response_headers"):
        header_value = header_lookup(getattr(candidate, attr_name, None), key)
        if header_value is not None:
            return header_value
    return None


def get_error_header(error: Exception, key: str) -> str | None:
    for candidate in iter_error_chain(error):
        header_value = _get_candidate_header(candidate, key)
        if header_value is not None:
            return header_value
    return None


def parse_retry_after_ms(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value) / 1000.0
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_retry_after_value(value: str | None) -> float | None:
    if value is None:
        return None

    try:
        parsed = float(value)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed >= 0 else None

    try:
        retry_datetime = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return max(retry_datetime.timestamp() - time.time(), 0.0)


def get_retry_after(error: Exception) -> float | None:
    for candidate in iter_error_chain(error):
        retry_after = parse_retry_after_ms(_get_candidate_header(candidate, "retry-after-ms"))
        if retry_after is not None:
            return retry_after

        retry_after = parse_retry_after_value(_get_candidate_header(candidate, "retry-after"))
        if retry_after is not None:
            return retry_after
    return None


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
