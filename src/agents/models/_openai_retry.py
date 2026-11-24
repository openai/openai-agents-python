from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest, ModelRetryNormalizedError
from ..tracing._model import current_model_span, record_retries
from ._retry_runtime import (
    get_error_code as _get_error_code,
    get_error_header as _get_header_value,
    get_request_id as _get_request_id,
    get_retry_after,
    get_status_code as _get_status_code,
    iter_error_chain as _iter_error_chain,
)


def record_openai_error_retries(
    client: AsyncOpenAI, error: Exception, create_kwargs: Mapping[str, Any]
) -> None:
    # A failed send has no raw response. The standard client marks its final
    # outgoing request unless the caller supplied or removed this header.
    if type(client) is not AsyncOpenAI or not isinstance(
        error, (APIStatusError, APIConnectionError)
    ):
        return
    header = "x-stainless-retry-count"
    overrides = create_kwargs.get("extra_headers") or {}
    if not isinstance(overrides, Mapping) or any(key.lower() == header for key in overrides):
        return
    count = error.request.headers.get(header, "")
    if (
        len(count) <= 10
        and count.isascii()
        and count.isdecimal()
        and int(count) <= client.max_retries
    ):
        record_retries(int(count))


async def create_with_retry_observation(
    client: AsyncOpenAI, resource: Any, create_kwargs: dict[str, Any]
) -> Any:
    create = resource.create
    if current_model_span() is None or not inspect.ismethod(create):
        return await create(**create_kwargs)
    raw_create = getattr(getattr(resource, "with_raw_response", None), "create", None)
    try:
        if not callable(raw_create):
            return await create(**create_kwargs)
        response = await raw_create(**create_kwargs)
    except (APIStatusError, APIConnectionError) as error:
        record_openai_error_retries(client, error, create_kwargs)
        raise
    record_retries(getattr(response, "retries_taken", 0))
    parsed = response.parse()
    return await parsed if inspect.isawaitable(parsed) else parsed


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

    retry_after = get_retry_after(error)

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
