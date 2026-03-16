from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from email.utils import parsedate_to_datetime
from inspect import isawaitable
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError

from ..items import ModelResponse, TResponseStreamEvent
from ..logger import logger
from ..models._retry_runtime import (
    provider_managed_retries_disabled,
    websocket_pre_event_retries_disabled,
)
from ..retry import (
    ModelRetryAdvice,
    ModelRetryAdviceRequest,
    ModelRetryBackoffInput,
    ModelRetryNormalizedError,
    ModelRetrySettings,
    RetryDecision,
    RetryPolicy,
    RetryPolicyContext,
    _coerce_backoff_settings,
    retry_policy_retries_safe_transport_errors,
)
from ..usage import RequestUsage, Usage

GetResponseCallable = Callable[[], Awaitable[ModelResponse]]
GetStreamCallable = Callable[[], AsyncIterator[TResponseStreamEvent]]
RewindCallable = Callable[[], Awaitable[None]]
GetRetryAdviceCallable = Callable[[ModelRetryAdviceRequest], ModelRetryAdvice | None]

DEFAULT_INITIAL_DELAY_SECONDS = 0.25
DEFAULT_MAX_DELAY_SECONDS = 2.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_JITTER = True
COMPATIBILITY_CONVERSATION_LOCKED_RETRIES = 3
_RETRY_SAFE_STREAM_EVENT_TYPES = frozenset({"response.created", "response.in_progress"})


def _iter_error_chain(error: Exception) -> Iterator[Exception]:
    current: Exception | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, Exception) else None


def _is_conversation_locked_error(error: Exception) -> bool:
    return (
        isinstance(error, BadRequestError) and getattr(error, "code", "") == "conversation_locked"
    )


def _get_header_value(headers: Any, key: str) -> str | None:
    normalized_key = key.lower()
    if isinstance(headers, httpx.Headers):
        value = headers.get(key)
        return value if isinstance(value, str) else None
    if isinstance(headers, Mapping):
        for header_name, header_value in headers.items():
            if str(header_name).lower() == normalized_key and isinstance(header_value, str):
                return header_value
    return None


def _extract_headers(error: Exception) -> httpx.Headers | Mapping[str, str] | None:
    for candidate in _iter_error_chain(error):
        response = getattr(candidate, "response", None)
        if isinstance(response, httpx.Response):
            return response.headers

        for attr_name in ("headers", "response_headers"):
            headers = getattr(candidate, attr_name, None)
            if isinstance(headers, (httpx.Headers, Mapping)):
                return headers

    return None


def _parse_retry_after(headers: httpx.Headers | Mapping[str, str] | None) -> float | None:
    if headers is None:
        return None

    retry_after_ms = _get_header_value(headers, "retry-after-ms")
    if retry_after_ms is not None:
        try:
            parsed_ms = float(retry_after_ms) / 1000.0
        except ValueError:
            parsed_ms = None
        if parsed_ms is not None and parsed_ms >= 0:
            return parsed_ms

    retry_after = _get_header_value(headers, "retry-after")
    if retry_after is None:
        return None

    try:
        parsed_seconds = float(retry_after)
    except ValueError:
        parsed_seconds = None
    if parsed_seconds is not None:
        return parsed_seconds if parsed_seconds >= 0 else None

    try:
        retry_datetime = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, IndexError):
        return None

    return max(retry_datetime.timestamp() - time.time(), 0.0)


def _get_status_code(error: Exception) -> int | None:
    for candidate in _iter_error_chain(error):
        if isinstance(candidate, APIStatusError):
            return candidate.status_code

        for attr_name in ("status_code", "status"):
            value = getattr(candidate, attr_name, None)
            if isinstance(value, int):
                return value

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


def _get_request_id(error: Exception) -> str | None:
    for candidate in _iter_error_chain(error):
        request_id = getattr(candidate, "request_id", None)
        if isinstance(request_id, str):
            return request_id
    return None


def _is_abort_like_error(error: Exception) -> bool:
    if isinstance(error, asyncio.CancelledError):
        return True

    for candidate in _iter_error_chain(error):
        if isinstance(candidate, asyncio.CancelledError):
            return True
        if candidate.__class__.__name__ in {"AbortError", "CancelledError"}:
            return True

    return False


def _is_network_like_error(error: Exception) -> bool:
    if isinstance(error, (APIConnectionError, APITimeoutError, TimeoutError)):
        return True

    network_error_types = (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.RemoteProtocolError,
        httpx.TimeoutException,
        httpx.WriteError,
    )
    if isinstance(error, network_error_types):
        return True

    for candidate in _iter_error_chain(error):
        if isinstance(candidate, network_error_types):
            return True
        if candidate.__class__.__module__.startswith(
            "websockets"
        ) and candidate.__class__.__name__.startswith("ConnectionClosed"):
            return True

    message = str(error).lower()
    return (
        "connection error" in message
        or "network error" in message
        or "socket hang up" in message
        or "connection closed" in message
    )


def _normalize_retry_error(
    error: Exception,
    provider_advice: ModelRetryAdvice | None,
) -> ModelRetryNormalizedError:
    normalized = ModelRetryNormalizedError(
        status_code=_get_status_code(error),
        error_code=_get_error_code(error),
        message=str(error),
        request_id=_get_request_id(error),
        retry_after=_parse_retry_after(_extract_headers(error)),
        is_abort=_is_abort_like_error(error),
        is_network_error=_is_network_like_error(error),
        is_timeout=any(
            isinstance(candidate, (APITimeoutError, TimeoutError))
            for candidate in _iter_error_chain(error)
        ),
    )

    if provider_advice is not None:
        if provider_advice.retry_after is not None:
            normalized.retry_after = provider_advice.retry_after
        if provider_advice.normalized is not None:
            override = provider_advice.normalized
            for field_name in (
                "status_code",
                "error_code",
                "message",
                "request_id",
                "retry_after",
                "is_abort",
                "is_network_error",
                "is_timeout",
            ):
                if field_name in getattr(override, "_explicit_fields", ()):
                    override_value = getattr(override, field_name)
                    setattr(normalized, field_name, override_value)

    return normalized


def _coerce_retry_decision(value: bool | RetryDecision) -> RetryDecision:
    if isinstance(value, RetryDecision):
        return value
    return RetryDecision(retry=bool(value))


async def _call_retry_policy(
    retry_policy: RetryPolicy,
    context: RetryPolicyContext,
) -> RetryDecision:
    decision = retry_policy(context)
    if isawaitable(decision):
        decision = await decision
    return _coerce_retry_decision(decision)


def _default_retry_delay(
    attempt: int,
    backoff: ModelRetryBackoffInput | None,
) -> float:
    backoff = _coerce_backoff_settings(backoff)
    initial_delay = (
        backoff.initial_delay
        if backoff is not None and backoff.initial_delay is not None
        else DEFAULT_INITIAL_DELAY_SECONDS
    )
    max_delay = (
        backoff.max_delay
        if backoff is not None and backoff.max_delay is not None
        else DEFAULT_MAX_DELAY_SECONDS
    )
    multiplier = (
        backoff.multiplier
        if backoff is not None and backoff.multiplier is not None
        else DEFAULT_BACKOFF_MULTIPLIER
    )
    use_jitter = (
        backoff.jitter
        if backoff is not None and backoff.jitter is not None
        else DEFAULT_BACKOFF_JITTER
    )

    base = min(initial_delay * (multiplier ** max(attempt - 1, 0)), max_delay)
    if not use_jitter:
        return base
    return min(max(base * (0.875 + random.random() * 0.25), 0.0), max_delay)


async def _sleep_for_retry(delay: float) -> None:
    if delay <= 0:
        return
    await asyncio.sleep(delay)


def _build_zero_request_usage_entry() -> RequestUsage:
    return RequestUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        input_tokens_details=Usage().input_tokens_details,
        output_tokens_details=Usage().output_tokens_details,
    )


def _build_request_usage_entry_from_usage(usage: Usage) -> RequestUsage:
    return RequestUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        input_tokens_details=usage.input_tokens_details,
        output_tokens_details=usage.output_tokens_details,
    )


def apply_retry_attempt_usage(usage: Usage, failed_attempts: int) -> Usage:
    if failed_attempts <= 0:
        return usage

    successful_request_entries = list(usage.request_usage_entries)
    if not successful_request_entries:
        successful_request_entries.append(_build_request_usage_entry_from_usage(usage))

    usage.requests = max(usage.requests, 1) + failed_attempts
    usage.request_usage_entries = [
        _build_zero_request_usage_entry() for _ in range(failed_attempts)
    ] + successful_request_entries
    return usage


async def _close_async_iterator(iterator: Any) -> None:
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        await aclose()
        return

    close = getattr(iterator, "close", None)
    if callable(close):
        close_result = close()
        if isawaitable(close_result):
            await close_result


async def _close_async_iterator_quietly(iterator: Any | None) -> None:
    if iterator is None:
        return

    try:
        await _close_async_iterator(iterator)
    except Exception as exc:
        logger.debug(f"Ignoring retry stream cleanup error: {exc}")


def _get_stream_event_type(event: TResponseStreamEvent) -> str | None:
    if isinstance(event, Mapping):
        event_type = event.get("type")
        return event_type if isinstance(event_type, str) else None
    event_type = getattr(event, "type", None)
    return event_type if isinstance(event_type, str) else None


def _stream_event_blocks_retry(event: TResponseStreamEvent) -> bool:
    event_type = _get_stream_event_type(event)
    return event_type not in _RETRY_SAFE_STREAM_EVENT_TYPES


async def _evaluate_retry(
    *,
    error: Exception,
    attempt: int,
    max_retries: int,
    retry_policy: RetryPolicy | None,
    retry_backoff: ModelRetryBackoffInput | None,
    stream: bool,
    replay_unsafe_request: bool,
    emitted_retry_unsafe_event: bool,
    provider_advice: ModelRetryAdvice | None,
) -> RetryDecision:
    if attempt > max_retries:
        return RetryDecision(retry=False)

    normalized = _normalize_retry_error(error, provider_advice)
    if (
        normalized.is_abort
        or emitted_retry_unsafe_event
        or (provider_advice is not None and provider_advice.replay_safety == "unsafe")
    ):
        return RetryDecision(
            retry=False, reason=provider_advice.reason if provider_advice else None
        )

    if retry_policy is None:
        return RetryDecision(retry=False)

    decision = await _call_retry_policy(
        retry_policy,
        RetryPolicyContext(
            error=error,
            attempt=attempt,
            max_retries=max_retries,
            stream=stream,
            normalized=normalized,
            provider_advice=provider_advice,
        ),
    )
    if not decision.retry:
        return decision

    provider_marks_replay_safe = (
        provider_advice is not None and provider_advice.replay_safety == "safe"
    )
    if replay_unsafe_request and not decision._approves_replay and not provider_marks_replay_safe:
        return RetryDecision(
            retry=False,
            reason=decision.reason or (provider_advice.reason if provider_advice else None),
        )

    return RetryDecision(
        retry=True,
        delay=(
            decision.delay
            if decision.delay is not None
            else (
                normalized.retry_after
                if normalized.retry_after is not None
                else _default_retry_delay(attempt, retry_backoff)
            )
        ),
        reason=decision.reason or (provider_advice.reason if provider_advice else None),
    )


def _is_stateful_request(
    *,
    previous_response_id: str | None,
    conversation_id: str | None,
) -> bool:
    return bool(previous_response_id or conversation_id)


def _should_preserve_conversation_locked_compatibility(
    retry_settings: ModelRetrySettings | None,
) -> bool:
    if retry_settings is None:
        return True
    max_retries = retry_settings.max_retries
    # Keep the legacy lock-retry behavior unless the caller explicitly opts out with
    # max_retries=0. This preserves historical behavior for callers enabling retry
    # policies for unrelated failures while still allowing an explicit disable.
    return max_retries is None or max_retries > 0


def _should_disable_provider_managed_retries(
    retry_settings: ModelRetrySettings | None,
    *,
    attempt: int,
    stateful_request: bool,
) -> bool:
    if (
        retry_settings is not None
        and retry_settings.max_retries is not None
        and retry_settings.max_retries <= 0
    ):
        # An explicit no-retry budget should also disable hidden provider retries so callers
        # can fully opt out of retries.
        return True

    if attempt > 1:
        if stateful_request:
            # Any stateful replay attempt already passed through runner rewind/safety decisions,
            # including conversation-locked compatibility retries that can run without a policy.
            return True
        if retry_settings is None or retry_settings.policy is None:
            # Without a policy, the runner never schedules stateless retries, so provider retries
            # remain the only transient-failure recovery path.
            return False
        return max(retry_settings.max_retries or 0, 0) > 0

    if retry_settings is None:
        return False
    if not stateful_request:
        # Keep provider-managed retries on the initial attempt for backward compatibility.
        return False

    max_retries = retry_settings.max_retries
    # Stateful requests must route replay decisions through the runner so hidden SDK retries
    # cannot resend conversation-bound deltas before rewind/replay-safety checks run.
    return max_retries is not None and max_retries > 0 and retry_settings.policy is not None


def _should_disable_websocket_pre_event_retry(
    retry_settings: ModelRetrySettings | None,
) -> bool:
    if retry_settings is None:
        return False
    if retry_settings.max_retries is not None and retry_settings.max_retries <= 0:
        return True
    if retry_settings.policy is None:
        return False
    max_retries = retry_settings.max_retries
    return (
        max_retries is not None
        and max_retries > 0
        and retry_policy_retries_safe_transport_errors(retry_settings.policy)
    )


async def get_response_with_retry(
    *,
    get_response: GetResponseCallable,
    rewind: RewindCallable,
    retry_settings: ModelRetrySettings | None,
    get_retry_advice: GetRetryAdviceCallable,
    previous_response_id: str | None,
    conversation_id: str | None,
) -> ModelResponse:
    request_attempt = 1
    policy_attempt = 1
    failed_policy_attempts = 0
    compatibility_retries_taken = 0
    disable_websocket_pre_event_retry = _should_disable_websocket_pre_event_retry(retry_settings)
    stateful_request = _is_stateful_request(
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
    )

    while True:
        try:
            # Keep provider retries on the initial attempt, but disable them on explicit
            # no-retry settings and on any replay attempt that the runner manages itself.
            with (
                provider_managed_retries_disabled(
                    _should_disable_provider_managed_retries(
                        retry_settings,
                        attempt=request_attempt,
                        stateful_request=stateful_request,
                    )
                ),
                websocket_pre_event_retries_disabled(disable_websocket_pre_event_retry),
            ):
                response = await get_response()
            response.usage = apply_retry_attempt_usage(
                response.usage,
                failed_policy_attempts + compatibility_retries_taken,
            )
            return response
        except Exception as error:
            if _is_conversation_locked_error(
                error
            ) and _should_preserve_conversation_locked_compatibility(retry_settings):
                # Preserve the historical conversation_locked retry path for backward
                # compatibility, including when callers enable retry policies for unrelated
                # failures. Callers can explicitly opt out of this compatibility behavior with
                # max_retries=0.
                if compatibility_retries_taken < COMPATIBILITY_CONVERSATION_LOCKED_RETRIES:
                    compatibility_retries_taken += 1
                    delay = 1.0 * (2 ** (compatibility_retries_taken - 1))
                    logger.debug(
                        "Conversation locked, retrying in %ss (attempt %s/%s).",
                        delay,
                        compatibility_retries_taken,
                        COMPATIBILITY_CONVERSATION_LOCKED_RETRIES,
                    )
                    await rewind()
                    await _sleep_for_retry(delay)
                    request_attempt += 1
                    continue

            provider_advice = get_retry_advice(
                ModelRetryAdviceRequest(
                    error=error,
                    attempt=policy_attempt,
                    stream=False,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                )
            )
            decision = await _evaluate_retry(
                error=error,
                attempt=policy_attempt,
                max_retries=max(retry_settings.max_retries or 0, 0) if retry_settings else 0,
                retry_policy=retry_settings.policy if retry_settings else None,
                retry_backoff=retry_settings.backoff if retry_settings else None,
                stream=False,
                replay_unsafe_request=stateful_request,
                emitted_retry_unsafe_event=False,
                provider_advice=provider_advice,
            )
            if not decision.retry:
                raise

            logger.debug(
                "Retrying failed model request in %ss (attempt %s/%s).",
                decision.delay,
                policy_attempt,
                retry_settings.max_retries
                if retry_settings and retry_settings.max_retries is not None
                else 0,
            )
            await rewind()
            await _sleep_for_retry(decision.delay or 0.0)
            request_attempt += 1
            policy_attempt += 1
            failed_policy_attempts += 1


async def stream_response_with_retry(
    *,
    get_stream: GetStreamCallable,
    rewind: RewindCallable,
    retry_settings: ModelRetrySettings | None,
    get_retry_advice: GetRetryAdviceCallable,
    previous_response_id: str | None,
    conversation_id: str | None,
    failed_retry_attempts_out: list[int] | None = None,
) -> AsyncIterator[TResponseStreamEvent]:
    request_attempt = 1
    policy_attempt = 1
    failed_policy_attempts = 0
    compatibility_retries_taken = 0
    disable_websocket_pre_event_retry = _should_disable_websocket_pre_event_retry(retry_settings)
    stateful_request = _is_stateful_request(
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
    )

    while True:
        emitted_retry_unsafe_event = False
        stream: AsyncIterator[TResponseStreamEvent] | None = None
        try:
            disable_provider_managed_retries = _should_disable_provider_managed_retries(
                retry_settings,
                attempt=request_attempt,
                stateful_request=stateful_request,
            )
            # Pull stream events under the retry-disable context, but yield them outside it so
            # unrelated model calls made by the consumer do not inherit this setting.
            with (
                provider_managed_retries_disabled(disable_provider_managed_retries),
                websocket_pre_event_retries_disabled(disable_websocket_pre_event_retry),
            ):
                stream = get_stream()
            while True:
                try:
                    with (
                        provider_managed_retries_disabled(disable_provider_managed_retries),
                        websocket_pre_event_retries_disabled(disable_websocket_pre_event_retry),
                    ):
                        event = await stream.__anext__()
                except StopAsyncIteration:
                    await _close_async_iterator_quietly(stream)
                    return
                if _stream_event_blocks_retry(event):
                    emitted_retry_unsafe_event = True
                if failed_retry_attempts_out is not None:
                    failed_retry_attempts_out[:] = [
                        failed_policy_attempts + compatibility_retries_taken
                    ]
                yield event
            return
        except BaseException as error:
            await _close_async_iterator_quietly(stream)
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                raise
            if not isinstance(error, Exception):
                raise
            if _is_conversation_locked_error(
                error
            ) and _should_preserve_conversation_locked_compatibility(retry_settings):
                if compatibility_retries_taken < COMPATIBILITY_CONVERSATION_LOCKED_RETRIES:
                    compatibility_retries_taken += 1
                    delay = 1.0 * (2 ** (compatibility_retries_taken - 1))
                    logger.debug(
                        (
                            "Conversation locked during streamed request, retrying in %ss "
                            "(attempt %s/%s)."
                        ),
                        delay,
                        compatibility_retries_taken,
                        COMPATIBILITY_CONVERSATION_LOCKED_RETRIES,
                    )
                    await rewind()
                    await _sleep_for_retry(delay)
                    request_attempt += 1
                    continue
            provider_advice = get_retry_advice(
                ModelRetryAdviceRequest(
                    error=error,
                    attempt=policy_attempt,
                    stream=True,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                )
            )
            decision = await _evaluate_retry(
                error=error,
                attempt=policy_attempt,
                max_retries=max(retry_settings.max_retries or 0, 0) if retry_settings else 0,
                retry_policy=retry_settings.policy if retry_settings else None,
                retry_backoff=retry_settings.backoff if retry_settings else None,
                stream=True,
                replay_unsafe_request=stateful_request,
                emitted_retry_unsafe_event=emitted_retry_unsafe_event,
                provider_advice=provider_advice,
            )
            if not decision.retry:
                raise

            logger.debug(
                "Retrying failed streamed model request in %ss (attempt %s/%s).",
                decision.delay,
                policy_attempt,
                retry_settings.max_retries
                if retry_settings and retry_settings.max_retries is not None
                else 0,
            )
            await rewind()
            await _sleep_for_retry(decision.delay or 0.0)
            request_attempt += 1
            policy_attempt += 1
            failed_policy_attempts += 1
