from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable, Coroutine, Iterable
from enum import Enum
from typing import ParamSpec, TypeVar, cast

P = ParamSpec("P")
T = TypeVar("T")


class BackoffStrategy(str, Enum):
    def __str__(self) -> str:
        return str(self.value)

    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


DEFAULT_TRANSIENT_RETRY_INTERVAL_S = 0.25
DEFAULT_TRANSIENT_RETRY_MAX_ATTEMPT = 3
DEFAULT_TRANSIENT_RETRY_BACKOFF = BackoffStrategy.EXPONENTIAL
TRANSIENT_HTTP_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504})


def iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        current = cast(
            BaseException | None,
            getattr(current, "__cause__", None) or getattr(current, "__context__", None),
        )


def exception_chain_contains_type(
    exc: BaseException,
    error_types: tuple[type[BaseException], ...],
) -> bool:
    if not error_types:
        return False
    return any(isinstance(candidate, error_types) for candidate in iter_exception_chain(exc))


def exception_chain_has_status_code(
    exc: BaseException,
    status_codes: set[int] | frozenset[int],
) -> bool:
    for candidate in iter_exception_chain(exc):
        for value in (
            getattr(candidate, "status_code", None),
            getattr(candidate, "http_code", None),
            getattr(getattr(candidate, "response", None), "status_code", None),
        ):
            if isinstance(value, int) and value in status_codes:
                return True
    return False


def retry_async(
    *,
    interval: float = DEFAULT_TRANSIENT_RETRY_INTERVAL_S,
    max_attempt: int = DEFAULT_TRANSIENT_RETRY_MAX_ATTEMPT,
    backoff: BackoffStrategy = DEFAULT_TRANSIENT_RETRY_BACKOFF,
    retry_if: Callable[..., bool],
    on_retry: Callable[..., object] | None = None,
) -> Callable[
    [Callable[P, Coroutine[object, object, T]]],
    Callable[P, Coroutine[object, object, T]],
]:
    """Retry an async function when `retry_if` marks the exception as transient.

    `backoff=BackoffStrategy.FIXED` keeps a constant delay equal to `interval`.
    `backoff=BackoffStrategy.LINEAR` scales delay as `interval * attempt`.
    `backoff=BackoffStrategy.EXPONENTIAL` doubles the delay on each retry attempt.
    """

    if max_attempt < 1:
        raise ValueError("max_attempt must be >= 1")
    if interval < 0:
        raise ValueError("interval must be >= 0")
    if backoff not in {
        BackoffStrategy.FIXED,
        BackoffStrategy.LINEAR,
        BackoffStrategy.EXPONENTIAL,
    }:
        raise ValueError(
            "backoff must be BackoffStrategy.FIXED, "
            "BackoffStrategy.LINEAR, or BackoffStrategy.EXPONENTIAL"
        )

    def decorator(
        fn: Callable[P, Coroutine[object, object, T]],
    ) -> Callable[P, Coroutine[object, object, T]]:
        @functools.wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, max_attempt + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    if attempt >= max_attempt or not retry_if(exc, *args, **kwargs):
                        raise

                    if backoff is BackoffStrategy.EXPONENTIAL:
                        delay_s = interval * (2 ** (attempt - 1))
                    elif backoff is BackoffStrategy.LINEAR:
                        delay_s = interval * attempt
                    else:
                        delay_s = interval

                    if on_retry is not None:
                        hook_result = on_retry(exc, attempt, max_attempt, delay_s, *args, **kwargs)
                        if inspect.isawaitable(hook_result):
                            await hook_result

                    await asyncio.sleep(delay_s)

            raise AssertionError("unreachable")

        return cast(Callable[P, Coroutine[object, object, T]], wrapped)

    return decorator
