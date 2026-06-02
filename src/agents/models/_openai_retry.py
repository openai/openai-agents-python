from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest, ModelRetryNormalizedError


def _iter_error_chain(error: Exception) -> Iterator[Exception]:
    current: Exception | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, Exception) else None


def _header_lookup(headers: Any, key: str) -> str | None:
    normalized_key = key.lower()
    if isinstance(headers, httpx.Headers):
        value = headers.get(key)
        return value if isinstance(value, str) else None
    if isinstance(headers, Mapping):
        for header_name, header_value in headers.items():
            if str(header_name).lower() == normalized_key and isinstance(header_value, str):
                return header_value
    return None


def _get_header_value(error: Exception, key: str) -> str | None:
    for candidate in _iter_error_chain(error):
        response = getattr(candidate, "response", None)
        if isinstance(response, httpx.Response):
            header_value = _header_lookup(response.headers, key)
            if header_value is not None:
                return header_value

        for attr_name in ("headers", "response_headers"):
            header_value = _header_lookup(getattr(candidate, attr_name, None), key)
            if header_value is not None:
                return header_value

    return None


def _parse_retry_after_ms(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value) / 1000.0
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_retry_after(value: str | None) -> float | None:
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


def _get_status_code(error: Exception) -> int | None:
    for candidate in _iter_error_chain(error):
        if isinstance(candidate, APIStatusError):
            return candidate.status_code
        status_code = getattr(candidate, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        status = getattr(candidate, "status", None)
        if isinstance(status, int):
            return status
    return None


def _get_request_id(error: Exception) -> str | None:
    for candidate in _iter_error_chain(error):
        request_id = getattr(candidate, "request_id", None)
        if isinstance(request_id, str):
            return request_id
    return None


def _get_error_code(error: Exception) -> str | None:
    for candidate in _iter_error_chain(error):
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


def _is_stateful_request(request: ModelRetryAdviceRequest) -> bool:
    return bool(request.previous_response_id or request.conversation_id)


def _build_normalized_error(
    error: Exception,
    *,
    retry_after: float | None,
) -> ModelRetryNormalizedError:
    return ModelRetryNormalizedError(
        status_code=_get_status_code(error),
        error_code=_get_error_code(error),
        message=str(error),
        request_id=_get_request_id(error),
        retry_after=retry_after,
        is_abort=False,
        is_network_error=any(
            isinstance(candidate, APIConnectionError) for candidate in _iter_error_chain(error)
        ),
        is_timeout=any(
            isinstance(candidate, APITimeoutError) for candidate in _iter_error_chain(error)
        ),
    )


def get_openai_retry_advice(request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
    error = request.error
    if getattr(error, "unsafe_to_replay", False):
        return ModelRetryAdvice(
            suggested=False,
            replay_safety="unsafe",
            reason=str(error),
        )

    error_message = str(error).lower()
    if (
        "the request may have been accepted, so the sdk will not automatically "
        "retry this websocket request." in error_message
    ):
        return ModelRetryAdvice(
            suggested=False,
            replay_safety="unsafe",
            reason=str(error),
        )

    retry_after = _parse_retry_after_ms(_get_header_value(error, "retry-after-ms"))
    if retry_after is None:
        retry_after = _parse_retry_after(_get_header_value(error, "retry-after"))

    normalized = _build_normalized_error(error, retry_after=retry_after)
    stateful_request = _is_stateful_request(request)
    should_retry_header = _get_header_value(error, "x-should-retry")
    if should_retry_header is not None:
        header_value = should_retry_header.lower().strip()
        if header_value == "true":
            return ModelRetryAdvice(
                suggested=True,
                retry_after=retry_after,
                replay_safety="safe",
                reason=str(error),
                normalized=normalized,
            )
        if header_value == "false":
            return ModelRetryAdvice(
                suggested=False,
                retry_after=retry_after,
                reason=str(error),
                normalized=normalized,
            )

    if normalized.is_network_error or normalized.is_timeout:
        return ModelRetryAdvice(
            suggested=True,
            retry_after=retry_after,
            reason=str(error),
            normalized=normalized,
        )

    if normalized.status_code in {408, 409, 429} or (
        isinstance(normalized.status_code, int) and normalized.status_code >= 500
    ):
        advice = ModelRetryAdvice(
            suggested=True,
            retry_after=retry_after,
            reason=str(error),
            normalized=normalized,
        )
        if stateful_request:
            advice.replay_safety = "safe"
        return advice

    if retry_after is not None:
        return ModelRetryAdvice(
            retry_after=retry_after,
            reason=str(error),
            normalized=normalized,
        )

    return None
