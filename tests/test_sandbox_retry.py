from __future__ import annotations

import asyncio
from typing import cast

import pytest

from agents.sandbox.util.retry import (
    BackoffStrategy,
    exception_chain_contains_type,
    exception_chain_has_status_code,
    iter_exception_chain,
    retry_async,
)


class _ErrorWithHttpMetadata(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        http_code: int | None = None,
        response_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.http_code = http_code
        if response_status_code is not None:
            self.response = type("_Response", (), {"status_code": response_status_code})()


def test_iter_exception_chain_supports_context_and_stops_on_cycles() -> None:
    outer = RuntimeError("outer")
    inner = ValueError("inner")
    outer.__context__ = inner

    assert list(iter_exception_chain(outer)) == [outer, inner]

    cyclical_outer = RuntimeError("cyclical-outer")
    cyclical_inner = ValueError("cyclical-inner")
    cyclical_outer.__cause__ = cyclical_inner
    cyclical_inner.__cause__ = cyclical_outer

    assert list(iter_exception_chain(cyclical_outer)) == [cyclical_outer, cyclical_inner]


def test_exception_chain_helpers_detect_types_and_status_codes() -> None:
    outer = RuntimeError("outer")
    inner = _ErrorWithHttpMetadata("inner", response_status_code=504)
    outer.__cause__ = inner

    assert exception_chain_contains_type(outer, ()) is False
    assert exception_chain_contains_type(outer, (_ErrorWithHttpMetadata,)) is True
    assert exception_chain_contains_type(outer, (LookupError,)) is False

    assert exception_chain_has_status_code(
        _ErrorWithHttpMetadata("status", status_code=500),
        {500},
    )
    assert exception_chain_has_status_code(
        _ErrorWithHttpMetadata("http", http_code=502),
        {502},
    )
    assert exception_chain_has_status_code(outer, {504})
    assert exception_chain_has_status_code(outer, {503}) is False


def test_retry_async_validates_configuration() -> None:
    with pytest.raises(ValueError, match="max_attempt must be >= 1"):
        retry_async(max_attempt=0, retry_if=lambda _exc: True)

    with pytest.raises(ValueError, match="interval must be >= 0"):
        retry_async(interval=-1, retry_if=lambda _exc: True)

    with pytest.raises(ValueError, match="backoff must be"):
        retry_async(
            backoff=cast(BackoffStrategy, "quadratic"),
            retry_if=lambda _exc: True,
        )


@pytest.mark.parametrize(
    ("backoff", "expected_delays"),
    [
        (BackoffStrategy.FIXED, [0.5, 0.5]),
        (BackoffStrategy.LINEAR, [0.5, 1.0]),
        (BackoffStrategy.EXPONENTIAL, [0.5, 1.0]),
    ],
)
@pytest.mark.asyncio
async def test_retry_async_retries_with_expected_backoff_and_async_hook(
    monkeypatch: pytest.MonkeyPatch,
    backoff: BackoffStrategy,
    expected_delays: list[float],
) -> None:
    sleep_delays: list[float] = []
    hook_calls: list[tuple[int, int, float]] = []
    attempts = 0

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    async def on_retry(
        _exc: Exception,
        attempt: int,
        max_attempt: int,
        delay_s: float,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        hook_calls.append((attempt, max_attempt, delay_s))

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    @retry_async(
        interval=0.5,
        max_attempt=3,
        backoff=backoff,
        retry_if=lambda exc, *_args, **_kwargs: isinstance(exc, RuntimeError),
        on_retry=on_retry,
    )
    async def flaky(label: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError(label)
        return f"ok:{label}"

    result = await flaky("sandbox")

    assert result == "ok:sandbox"
    assert attempts == 3
    assert sleep_delays == expected_delays
    assert hook_calls == [(1, 3, expected_delays[0]), (2, 3, expected_delays[1])]
    assert str(backoff) == backoff.value


@pytest.mark.asyncio
async def test_retry_async_stops_without_sleep_when_retry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def fail_sleep(_delay: float) -> None:
        raise AssertionError("sleep should not be called")

    monkeypatch.setattr(asyncio, "sleep", fail_sleep)

    @retry_async(
        interval=0.5,
        max_attempt=3,
        backoff=BackoffStrategy.EXPONENTIAL,
        retry_if=lambda _exc, *_args, **_kwargs: False,
        on_retry=lambda *_args, **_kwargs: None,
    )
    async def always_fail() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        await always_fail()

    assert attempts == 1
