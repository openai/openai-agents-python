from __future__ import annotations

import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from openai import APIConnectionError, APITimeoutError

from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest, ModelRetryNormalizedError
from ._retry_runtime import (
    get_error_code as _get_error_code,
    get_request_id as _get_request_id,
    get_status_code as _get_status_code,
    iter_error_chain as _iter_error_chain,
)


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
