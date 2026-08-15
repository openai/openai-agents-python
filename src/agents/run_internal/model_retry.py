from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from inspect import isawaitable
from typing import Any, TypeVar

import httpx2
from openai import APIConnectionError, APITimeoutError, BadRequestError

from .._httpx_compat import is_legacy_httpx_instance
from ..exceptions import ModelTimeoutError
from ..items import ModelResponse, TResponseStreamEvent
from ..logger import log_model_action_debug, logger
from ..models._retry_runtime import (
    get_error_code as _get_error_code,
    get_request_id as _get_request_id,
    get_retry_after as _get_retry_after,
    get_status_code as _get_status_code,
    iter_error_chain as _iter_error_chain,
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
from ..util._error_tracing import mark_model_timeout_task

GetResponseCallable = Callable[[], Awaitable[ModelResponse]]
GetStreamCallable = Callable[[], AsyncIterator[TResponseStreamEvent]]
RewindCallable = Callable[[], Awaitable[None]]
GetRetryAdviceCallable = Callable[[ModelRetryAdviceRequest], ModelRetryAdvice | None]
T = TypeVar("T")

DEFAULT_INITIAL_DELAY_SECONDS = 0.25
DEFAULT_MAX_DELAY_SECONDS = 2.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_JITTER = True
COMPATIBILITY_CONVERSATION_LOCKED_RETRIES = 3
_RETRY_SAFE_STREAM_EVENT_TYPES = frozenset({"response.created", "response.in_progress"})
_NETWORK_ERROR_TYPES = (
    httpx2.ConnectError,
    httpx2.ReadError,
    httpx2.RemoteProtocolError,
    httpx2.TimeoutException,
    httpx2.WriteError,
)
_LEGACY_NETWORK_ERROR_TYPE_NAMES = (
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
    "TimeoutException",
    "WriteError",
)


def _is_conversation_locked_error(error: Exception) -> bool:
    return (
        isinstance(error, BadRequestError) and getattr(error, "code", "") == "conversation_locked"
    )


def _is_abort_like_error(error: Exception) -> bool:
    if isinstance(error, ModelTimeoutError):
        return False

    if isinstance(error, asyncio.CancelledError):
        return True

    for candidate in _iter_error_chain(error):
        if isinstance(candidate, asyncio.CancelledError):
            return True
        if candidate.__class__.__name__ in {"AbortError", "CancelledError"}:
            return True

    return False


def _is_network_like_error(error: Exception) -> bool:
    if isinstance(error, APIConnectionError | APITimeoutError | TimeoutError):
        return True

    if isinstance(error, _NETWORK_ERROR_TYPES):
        return True

    for candidate in _iter_error_chain(error):
        if isinstance(candidate, _NETWORK_ERROR_TYPES):
            return True
        if is_legacy_httpx_instance(candidate, *_LEGACY_NETWORK_ERROR_TYPE_NAMES):
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
        retry_after=_get_retry_after(error),
        is_abort=_is_abort_like_error(error),
        is_network_error=_is_network_like_error(error),
        is_timeout=any(
            isinstance(candidate, APITimeoutError | TimeoutError | ModelTimeoutError)
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
                "is_network_error",
                "is_timeout",
            ):
                if field_name in getattr(override, "_explicit_fields", ()):
                    override_value = getattr(override, field_name)
                    setattr(normalized, field_name, override_value)
            if "is_abort" in getattr(override, "_explicit_fields", ()):
                # Provider normalization may add abort evidence but cannot clear an abort
                # inferred from the raw exception.
                normalized.is_abort = normalized.is_abort or override.is_abort

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


async def _drain_model_attempt_task(task: asyncio.Future[Any]) -> BaseException | None:
    """Wait for a cancelled model task without letting its outcome replace cancellation."""
    try:
        await task
    except BaseException as error:
        return error
    return None


async def _await_cleanup_ignoring_cancellation(
    cleanup_task: asyncio.Task[BaseException | None],
) -> BaseException | None:
    """Finish cooperative cleanup before restoring an already-received parent cancellation."""
    while True:
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            if cleanup_task.done():
                return cleanup_task.result()


async def _cancel_and_drain_model_attempt_task(
    task: asyncio.Future[Any],
    timeout_error: ModelTimeoutError | None = None,
    *,
    cancel_cleanup: bool = False,
) -> BaseException | None:
    if timeout_error is not None:
        mark_model_timeout_task(task, timeout_error)
    task.cancel()
    if cancel_cleanup:
        # Give a cancelled model operation one event-loop turn to enter its owner-owned cleanup,
        # then interrupt a cooperative cleanup wait that must not outlive the SDK deadline.
        # Caller-owned cancellation and early consumer close do not opt into this second cancel.
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
    cleanup_task = asyncio.create_task(_drain_model_attempt_task(task))
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await _await_cleanup_ignoring_cancellation(cleanup_task)
        raise


async def _run_stream_attempt_in_one_task(
    get_stream: GetStreamCallable,
    requests: asyncio.Queue[None],
    results: asyncio.Queue[tuple[str, TResponseStreamEvent | BaseException | None]],
) -> None:
    """Own stream construction, pulls, and cleanup in one task context."""
    stream: AsyncIterator[TResponseStreamEvent] | None = None
    terminal_result: tuple[str, TResponseStreamEvent | BaseException | None] | None = None
    try:
        stream = get_stream()
        while True:
            await requests.get()
            try:
                event = await stream.__anext__()
            except StopAsyncIteration:
                terminal_result = ("done", None)
                break
            except BaseException as error:
                terminal_result = ("error", error)
                break
            results.put_nowait(("event", event))
    except BaseException as error:
        terminal_result = ("error", error)
    finally:
        if stream is not None:
            await _close_async_iterator_quietly(stream)
    if terminal_result is not None:
        results.put_nowait(terminal_result)


async def _await_model_attempt(
    awaitable: Awaitable[T],
    timeout: float | None,
    *,
    timeout_error_seconds: float | None = None,
) -> T:
    """Await one model operation and turn only this deadline into a timeout error."""
    if timeout is None:
        return await awaitable

    task = asyncio.ensure_future(awaitable)
    try:
        done, pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_and_drain_model_attempt_task(task)
        raise

    if task in done:
        return await task

    timeout_error = ModelTimeoutError(
        timeout_error_seconds if timeout_error_seconds is not None else timeout
    )
    await _cancel_and_drain_model_attempt_task(task, timeout_error, cancel_cleanup=True)
    # A traceback retains this frame's locals. Discard every reference that can keep the
    # cancelled provider task, its cleanup exception, or provider payload locals reachable.
    del awaitable, task, done, pending
    raise timeout_error from None


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
        log_model_action_debug(logger, "Ignoring retry stream cleanup error", exc)


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
    previous_response_id: str | None = None,
    conversation_id: str | None = None,
) -> RetryDecision:
    if attempt > max_retries:
        return RetryDecision(retry=False)

    normalized = _normalize_retry_error(error, provider_advice)
    context = RetryPolicyContext(
        error=error,
        attempt=attempt,
        max_retries=max_retries,
        stream=stream,
        normalized=normalized,
        provider_advice=provider_advice,
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
    )
    provider_marks_replay_unsafe = context.replay_safety == "unsafe"
    provider_marks_replay_safe = context.replay_safety == "safe"
    # Aborts, and failures that already emitted user-visible streamed output, are absolute
    # vetoes. No application decision can make replaying those safe.
    if normalized.is_abort or emitted_retry_unsafe_event:
        return RetryDecision(
            retry=False,
            reason=provider_advice.reason if provider_advice is not None else None,
        )
    # A provider-unsafe streamed failure and a request with a separate local-side-effect veto
    # stay blocked before the policy runs. Only a non-streamed request without that separate
    # veto can ask the application to approve provider-side replay risk.
    if provider_marks_replay_unsafe and (stream or replay_unsafe_request):
        return RetryDecision(
            retry=False,
            reason=provider_advice.reason if provider_advice is not None else None,
        )

    if retry_policy is None:
        return RetryDecision(retry=False)

    decision = await _call_retry_policy(retry_policy, context)
    if not decision.retry:
        return decision

    stateful_request = bool(previous_response_id or conversation_id)
    # Three separate vetoes, deliberately not folded together.
    #
    # 1. A request-level replay veto (Programmatic Tool Calling, for example) covers
    #    application-local side effects that may already have run. That is outside what
    #    `approve_unsafe_replay` authorizes, so only the provider-owned approval lifts it.
    if replay_unsafe_request and not decision._approves_replay and not provider_marks_replay_safe:
        return RetryDecision(
            retry=False,
            reason=decision.reason
            or (provider_advice.reason if provider_advice is not None else None),
        )
    # 2. A stateful request (`previous_response_id` / `conversation_id`, including the
    #    `auto_previous_response_id` case) fails closed by default because the follow-up
    #    depends on server-side state. It carries no application-local side effects, so an
    #    application approval can accept it — but only for the provider-marked unsafe failure
    #    this option is scoped to. When replay safety is unknown the request stays blocked:
    #    there is no provider-unsafe failure for the approval to be about.
    if stateful_request and not (
        decision._approves_replay
        or provider_marks_replay_safe
        or (decision.approve_unsafe_replay and provider_marks_replay_unsafe)
    ):
        return RetryDecision(
            retry=False,
            reason=decision.reason
            or (provider_advice.reason if provider_advice is not None else None),
        )
    # 3. Provider-marked replay unsafety is the case `approve_unsafe_replay` exists for.
    #    Both approvals are explicit; an ordinary `retry=True` is neither.
    if provider_marks_replay_unsafe and not (
        decision._approves_replay or decision.approve_unsafe_replay
    ):
        return RetryDecision(
            retry=False,
            reason=decision.reason
            or (provider_advice.reason if provider_advice is not None else None),
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
        reason=decision.reason or (provider_advice.reason if provider_advice is not None else None),
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
    replay_unsafe_request: bool,
) -> bool:
    if replay_unsafe_request:
        return True
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
    timeout: float | None = None,
    replay_unsafe_request: bool = False,
) -> ModelResponse:
    request_attempt = 1
    policy_attempt = 1
    failed_policy_attempts = 0
    compatibility_retries_taken = 0
    disable_websocket_pre_event_retry = replay_unsafe_request or (
        _should_disable_websocket_pre_event_retry(retry_settings)
    )
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
                        replay_unsafe_request=replay_unsafe_request,
                    )
                ),
                websocket_pre_event_retries_disabled(disable_websocket_pre_event_retry),
            ):
                response = await _await_model_attempt(get_response(), timeout)
            response.usage = apply_retry_attempt_usage(
                response.usage,
                failed_policy_attempts + compatibility_retries_taken,
            )
            return response
        except Exception as error:
            if (
                not replay_unsafe_request
                and _is_conversation_locked_error(error)
                and _should_preserve_conversation_locked_compatibility(retry_settings)
            ):
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
                max_retries=(
                    max(retry_settings.max_retries or 0, 0) if retry_settings is not None else 0
                ),
                retry_policy=retry_settings.policy if retry_settings is not None else None,
                retry_backoff=retry_settings.backoff if retry_settings is not None else None,
                stream=False,
                replay_unsafe_request=replay_unsafe_request,
                emitted_retry_unsafe_event=False,
                provider_advice=provider_advice,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
            )
            if not decision.retry:
                raise

            logger.debug(
                "Retrying failed model request in %ss (attempt %s/%s).",
                decision.delay,
                policy_attempt,
                retry_settings.max_retries
                if retry_settings is not None and retry_settings.max_retries is not None
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
    timeout: float | None = None,
    failed_retry_attempts_out: list[int] | None = None,
    replay_unsafe_request: bool = False,
) -> AsyncGenerator[TResponseStreamEvent, None]:
    request_attempt = 1
    policy_attempt = 1
    failed_policy_attempts = 0
    compatibility_retries_taken = 0
    disable_websocket_pre_event_retry = replay_unsafe_request or (
        _should_disable_websocket_pre_event_retry(retry_settings)
    )
    stateful_request = _is_stateful_request(
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
    )

    while True:
        emitted_retry_unsafe_event = False
        stream: AsyncIterator[TResponseStreamEvent] | None = None
        stream_owner: asyncio.Task[None] | None = None
        deadline = asyncio.get_running_loop().time() + timeout if timeout is not None else None
        try:
            disable_provider_managed_retries = _should_disable_provider_managed_retries(
                retry_settings,
                attempt=request_attempt,
                stateful_request=stateful_request,
                replay_unsafe_request=replay_unsafe_request,
            )
            stream_requests: asyncio.Queue[None] | None = None
            stream_results: (
                asyncio.Queue[tuple[str, TResponseStreamEvent | BaseException | None]] | None
            ) = None
            if timeout is None:
                # Pull stream events under the retry-disable context, but yield them outside it
                # so unrelated model calls made by the consumer do not inherit this setting.
                with (
                    provider_managed_retries_disabled(disable_provider_managed_retries),
                    websocket_pre_event_retries_disabled(disable_websocket_pre_event_retry),
                ):
                    stream = get_stream()
            else:
                stream_requests = asyncio.Queue()
                stream_results = asyncio.Queue()
                with (
                    provider_managed_retries_disabled(disable_provider_managed_retries),
                    websocket_pre_event_retries_disabled(disable_websocket_pre_event_retry),
                ):
                    stream_owner = asyncio.create_task(
                        _run_stream_attempt_in_one_task(
                            get_stream,
                            stream_requests,
                            stream_results,
                        )
                    )
            try:
                while True:
                    try:
                        if timeout is None:
                            assert stream is not None
                            with (
                                provider_managed_retries_disabled(disable_provider_managed_retries),
                                websocket_pre_event_retries_disabled(
                                    disable_websocket_pre_event_retry
                                ),
                            ):
                                event = await stream.__anext__()
                        else:
                            assert stream_owner is not None
                            assert stream_requests is not None
                            assert stream_results is not None
                            assert deadline is not None
                            stream_requests.put_nowait(None)
                            remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
                            try:
                                result_kind, result_value = await _await_model_attempt(
                                    stream_results.get(),
                                    remaining,
                                    timeout_error_seconds=timeout,
                                )
                            except ModelTimeoutError as error:
                                await _cancel_and_drain_model_attempt_task(
                                    stream_owner,
                                    error,
                                    cancel_cleanup=True,
                                )
                                raise
                            except asyncio.CancelledError:
                                await _cancel_and_drain_model_attempt_task(stream_owner)
                                raise
                            if result_kind == "done":
                                remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
                                await _await_model_attempt(
                                    stream_owner,
                                    remaining,
                                    timeout_error_seconds=timeout,
                                )
                                return
                            if result_kind == "error":
                                remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
                                await _await_model_attempt(
                                    stream_owner,
                                    remaining,
                                    timeout_error_seconds=timeout,
                                )
                                assert isinstance(result_value, BaseException)
                                raise result_value
                            assert result_kind == "event"
                            assert result_value is not None
                            assert not isinstance(result_value, BaseException)
                            event = result_value
                    except StopAsyncIteration:
                        if stream_owner is None:
                            await _close_async_iterator_quietly(stream)
                        return
                    if _stream_event_blocks_retry(event):
                        emitted_retry_unsafe_event = True
                    if failed_retry_attempts_out is not None:
                        failed_retry_attempts_out[:] = [
                            failed_policy_attempts + compatibility_retries_taken
                        ]
                    yield event
            finally:
                if stream_owner is not None and not stream_owner.done():
                    await _cancel_and_drain_model_attempt_task(stream_owner)
            return
        except BaseException as error:
            if isinstance(error, ModelTimeoutError):
                # The timed owner has already been cancelled and drained. Do not retain its
                # task or result queues in the public timeout traceback frame.
                stream = None
                stream_owner = None
                stream_requests = None
                stream_results = None
            if stream_owner is None:
                await _close_async_iterator_quietly(stream)
            if isinstance(error, asyncio.CancelledError | GeneratorExit):
                raise
            if not isinstance(error, Exception):
                raise
            if (
                not replay_unsafe_request
                and _is_conversation_locked_error(error)
                and _should_preserve_conversation_locked_compatibility(retry_settings)
            ):
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
                max_retries=(
                    max(retry_settings.max_retries or 0, 0) if retry_settings is not None else 0
                ),
                retry_policy=retry_settings.policy if retry_settings is not None else None,
                retry_backoff=retry_settings.backoff if retry_settings is not None else None,
                stream=True,
                replay_unsafe_request=replay_unsafe_request,
                emitted_retry_unsafe_event=emitted_retry_unsafe_event,
                provider_advice=provider_advice,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
            )
            if not decision.retry:
                raise

            logger.debug(
                "Retrying failed streamed model request in %ss (attempt %s/%s).",
                decision.delay,
                policy_attempt,
                retry_settings.max_retries
                if retry_settings is not None and retry_settings.max_retries is not None
                else 0,
            )
            await rewind()
            await _sleep_for_retry(decision.delay or 0.0)
            request_attempt += 1
            policy_attempt += 1
            failed_policy_attempts += 1
