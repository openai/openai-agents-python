from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, BadRequestError

from agents.items import ModelResponse, TResponseStreamEvent
from agents.models._openai_retry import get_openai_retry_advice
from agents.models._retry_runtime import (
    should_disable_provider_managed_retries,
    should_disable_websocket_pre_event_retries,
)
from agents.retry import (
    ModelRetryAdvice,
    ModelRetryBackoffSettings,
    ModelRetryNormalizedError,
    ModelRetrySettings,
    RetryDecision,
    RetryPolicyContext,
    retry_policies,
)
from agents.run_internal.model_retry import get_response_with_retry, stream_response_with_retry
from agents.usage import Usage

from .test_responses import get_text_message


def _connection_error(message: str = "connection error") -> APIConnectionError:
    return APIConnectionError(
        message=message,
        request=httpx.Request("POST", "https://example.com"),
    )


def _conversation_locked_error() -> BadRequestError:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "conversation_locked", "message": "locked"}},
    )
    error = BadRequestError(
        "locked",
        response=response,
        body={"error": {"code": "conversation_locked"}},
    )
    error.code = "conversation_locked"
    return error


def _status_error(status_code: int, code: str = "server_error") -> APIStatusError:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(
        status_code,
        request=request,
        json={"error": {"code": code, "message": code}},
    )
    error = APIStatusError(
        code,
        response=response,
        body={"error": {"code": code, "message": code}},
    )
    error.code = code
    return error


def _status_error_without_code(status_code: int, body_code: str = "server_error") -> APIStatusError:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(
        status_code,
        request=request,
        json={"error": {"code": body_code, "message": body_code}},
    )
    return APIStatusError(
        body_code,
        response=response,
        body={"error": {"code": body_code, "message": body_code}},
    )


class _AcloseTrackingStream:
    def __init__(
        self,
        events: list[TResponseStreamEvent] | None = None,
        *,
        error_before_yield: Exception | None = None,
    ) -> None:
        self._events = list(events or [])
        self._error_before_yield = error_before_yield
        self.aclose_calls = 0

    def __aiter__(self) -> _AcloseTrackingStream:
        return self

    async def __anext__(self) -> TResponseStreamEvent:
        if self._error_before_yield is not None:
            error = self._error_before_yield
            self._error_before_yield = None
            raise error
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _CloseTrackingStream:
    def __init__(self, events: list[TResponseStreamEvent]) -> None:
        self._events = list(events)
        self.close_calls = 0

    def __aiter__(self) -> _CloseTrackingStream:
        return self

    async def __anext__(self) -> TResponseStreamEvent:
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_get_response_with_retry_retries_and_augments_usage(monkeypatch) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_123",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            backoff=ModelRetryBackoffSettings(initial_delay=0.5, jitter=False),
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert sleeps == [0.5]
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_get_response_with_retry_keeps_provider_retries_on_first_attempt(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_provider_retry_flag",
        )

    await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert provider_retry_flags == [False, True]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_provider_retries_on_first_stateful_provider_hint(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_stateful_provider_retry_flag",
        )

    await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.provider_suggested(),
        ),
        get_retry_advice=lambda _request: ModelRetryAdvice(
            suggested=True,
            replay_safety="safe",
        ),
        previous_response_id="resp_prev",
        conversation_id=None,
    )

    assert provider_retry_flags == [True, True]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_stateful_provider_retries_with_narrow_policy(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Unrelated policy should not trigger runner rewind")

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.http_status([429]),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        )

    assert calls == 1
    assert provider_retry_flags == [True]


@pytest.mark.asyncio
async def test_get_response_with_retry_keeps_stateful_provider_retries_when_budget_omitted(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Omitted retry budget should not trigger runner rewind")

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        )

    assert calls == 1
    assert provider_retry_flags == [False]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_stateful_provider_retries_for_network_only_policy(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Stateful requests should not leave hidden provider retries enabled")

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        raise _status_error(500)

    with pytest.raises(APIStatusError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        )

    assert calls == 1
    assert provider_retry_flags == [True]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_stateful_provider_retries_for_partial_policy(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Stateful requests should not leave hidden provider retries enabled")

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        raise _status_error(429, code="rate_limit_exceeded")

    with pytest.raises(APIStatusError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.any(
                    retry_policies.network_error(),
                    retry_policies.http_status([500]),
                ),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        )

    assert calls == 1
    assert provider_retry_flags == [True]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_provider_retries_when_explicitly_disabled(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_provider_retry_preserved",
        )

    await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=0,
            policy=retry_policies.never(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 1
    assert provider_retry_flags == [True]


@pytest.mark.asyncio
async def test_get_response_with_retry_keeps_provider_retries_without_runner_policy(
    monkeypatch,
) -> None:
    calls = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_provider_retry_without_policy",
        )

    await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=2,
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 1
    assert provider_retry_flags == [False]


@pytest.mark.asyncio
async def test_get_response_with_retry_preserves_successful_request_usage_entry(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(
                requests=1,
                input_tokens=11,
                output_tokens=7,
                total_tokens=18,
            ),
            response_id="resp_usage_entries",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            backoff=ModelRetryBackoffSettings(jitter=False),
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert result.usage.requests == 2
    assert len(result.usage.request_usage_entries) == 2
    assert result.usage.request_usage_entries[0].total_tokens == 0
    assert result.usage.request_usage_entries[1].input_tokens == 11
    assert result.usage.request_usage_entries[1].output_tokens == 7
    assert result.usage.request_usage_entries[1].total_tokens == 18


@pytest.mark.asyncio
async def test_get_response_with_retry_preserves_zero_token_successful_request_usage_entry(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_zero_usage_entries",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            backoff=ModelRetryBackoffSettings(jitter=False),
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert result.usage.requests == 2
    assert len(result.usage.request_usage_entries) == 2
    assert result.usage.request_usage_entries[0].total_tokens == 0
    assert result.usage.request_usage_entries[1].total_tokens == 0


@pytest.mark.asyncio
async def test_get_response_with_retry_preserves_inferred_normalized_error_flags() -> None:
    calls = 0

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_partial_normalized",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            backoff=ModelRetryBackoffSettings(jitter=False),
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: ModelRetryAdvice(
            normalized=ModelRetryNormalizedError(status_code=429)
        ),
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 2
    assert result.response_id == "resp_partial_normalized"


@pytest.mark.asyncio
async def test_get_response_with_retry_honors_explicit_false_provider_normalized_override() -> None:
    calls = 0

    async def rewind() -> None:
        raise AssertionError("Explicit false override should suppress retries")

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                normalized=ModelRetryNormalizedError(
                    is_network_error=False,
                    is_timeout=False,
                )
            ),
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_get_response_with_retry_honors_explicit_none_retry_after_override() -> None:
    calls = 0

    async def rewind() -> None:
        raise AssertionError("Explicit retry_after=None should suppress retry-after retries")

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(
            429,
            request=request,
            headers={"retry-after-ms": "1250"},
            json={"error": {"code": "rate_limit", "message": "rate_limit"}},
        )
        raise APIStatusError(
            "rate_limit",
            response=response,
            body={"error": {"code": "rate_limit", "message": "rate_limit"}},
        )

    with pytest.raises(APIStatusError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(jitter=False),
                policy=retry_policies.retry_after(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                normalized=ModelRetryNormalizedError(retry_after=None),
            ),
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_get_response_with_retry_preserves_conversation_locked_compatibility(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _conversation_locked_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1, input_tokens=3, output_tokens=2, total_tokens=5),
            response_id="resp_compat",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=None,
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert sleeps == [1.0]
    assert result.usage.requests == 2
    assert len(result.usage.request_usage_entries) == 2
    assert result.usage.request_usage_entries[0].total_tokens == 0
    assert result.usage.request_usage_entries[1].total_tokens == 5


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_provider_retries_on_stateful_compat_replay(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0
    provider_retry_flags: list[bool] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        provider_retry_flags.append(should_disable_provider_managed_retries())
        calls += 1
        if calls == 1:
            raise _conversation_locked_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_stateful_compat_disable_none",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=None,
        get_retry_advice=lambda _request: None,
        previous_response_id="resp_prev",
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert provider_retry_flags == [False, True]
    assert sleeps == [1.0]
    assert result.response_id == "resp_stateful_compat_disable_none"


@pytest.mark.asyncio
async def test_get_response_with_retry_respects_explicit_disable_for_conversation_locked(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _conversation_locked_error()

    with pytest.raises(BadRequestError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=0,
                policy=retry_policies.never(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 1
    assert rewinds == 0
    assert sleeps == []


@pytest.mark.asyncio
async def test_get_response_with_retry_keeps_conversation_locked_compatibility_with_retry(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _conversation_locked_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_locked_retry_enabled",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert sleeps == [1.0]
    assert result.response_id == "resp_locked_retry_enabled"


@pytest.mark.asyncio
async def test_get_response_with_retry_allows_stateful_retry_when_provider_marks_safe(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_stateful",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.provider_suggested(),
        ),
        get_retry_advice=lambda _request: ModelRetryAdvice(
            suggested=True,
            replay_safety="safe",
        ),
        previous_response_id="resp_prev",
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_get_response_with_retry_allows_stateful_retry_for_http_failure_advice(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _status_error_without_code(429, "rate_limit")
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_stateful_http_failure",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.provider_suggested(),
        ),
        get_retry_advice=get_openai_retry_advice,
        previous_response_id="resp_prev",
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert result.response_id == "resp_stateful_http_failure"
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_get_response_with_retry_allows_provider_safe_stateful_retry_for_generic_policy(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_stateful_generic_policy",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: ModelRetryAdvice(
            suggested=True,
            replay_safety="safe",
        ),
        previous_response_id="resp_prev",
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_get_response_with_retry_rejects_stateful_retry_without_replay_safety() -> None:
    calls = 0

    async def rewind() -> None:
        raise AssertionError("State should not rewind when replay is vetoed")

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(jitter=False),
                policy=retry_policies.provider_suggested(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(suggested=True),
            previous_response_id="resp_prev",
            conversation_id=None,
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_get_response_with_retry_exposes_provider_error_code_to_retry_policies(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _status_error_without_code(429, "rate_limit")
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_rate_limit_retry",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            backoff=ModelRetryBackoffSettings(jitter=False),
            policy=lambda context: context.normalized.error_code == "rate_limit",
        ),
        get_retry_advice=get_openai_retry_advice,
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert result.response_id == "resp_rate_limit_retry"
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_get_response_with_retry_stops_after_retry_budget_exhausted(monkeypatch) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(initial_delay=0.5, jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 2
    assert rewinds == 1
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_get_response_with_retry_caps_conversation_locked_compatibility_retries(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _conversation_locked_error()

    with pytest.raises(BadRequestError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=None,
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 4
    assert rewinds == 3
    assert sleeps == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_get_response_with_retry_prefers_retry_after_over_backoff(monkeypatch) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=0),
            response_id="resp_retry_after",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            backoff=ModelRetryBackoffSettings(initial_delay=5.0, jitter=False),
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: ModelRetryAdvice(suggested=True, retry_after=1.75),
        previous_response_id=None,
        conversation_id=None,
    )

    assert rewinds == 1
    assert sleeps == [1.75]
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_get_response_with_retry_honors_provider_hard_veto() -> None:
    calls = 0

    async def rewind() -> None:
        raise AssertionError("Provider veto should stop retries before rewinding state")

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.any(
                    retry_policies.provider_suggested(),
                    retry_policies.network_error(),
                ),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                suggested=False, reason="server veto"
            ),
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_get_response_with_retry_allows_custom_policy_to_override_provider_veto(
    monkeypatch,
) -> None:
    calls = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _status_error_without_code(429, "rate_limit")
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_custom_policy_override",
        )

    result = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.retry_after(),
        ),
        get_retry_advice=lambda _request: ModelRetryAdvice(
            suggested=False,
            retry_after=1.75,
            reason="server veto",
            normalized=ModelRetryNormalizedError(
                status_code=429,
                retry_after=1.75,
            ),
        ),
        previous_response_id=None,
        conversation_id=None,
    )

    assert calls == 2
    assert rewinds == 1
    assert sleeps == [1.75]
    assert result.usage.requests == 2


@pytest.mark.asyncio
async def test_retry_policies_any_merges_later_positive_metadata() -> None:
    raw_decision = retry_policies.any(
        retry_policies.network_error(),
        retry_policies.retry_after(),
    )(
        RetryPolicyContext(
            error=_connection_error(),
            attempt=1,
            max_retries=2,
            stream=False,
            normalized=ModelRetryNormalizedError(
                is_network_error=True,
                retry_after=1.75,
            ),
            provider_advice=ModelRetryAdvice(retry_after=1.75),
        )
    )
    decision = await raw_decision if asyncio.iscoroutine(raw_decision) else raw_decision

    assert isinstance(decision, RetryDecision)
    assert decision.retry is True
    assert decision.delay == 1.75


@pytest.mark.asyncio
async def test_get_response_with_retry_honors_unsafe_replay_veto() -> None:
    calls = 0

    async def rewind() -> None:
        raise AssertionError("Unsafe replay should not rewind state")

    async def get_response() -> ModelResponse:
        nonlocal calls
        calls += 1
        raise _connection_error()

    with pytest.raises(APIConnectionError):
        await get_response_with_retry(
            get_response=get_response,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                suggested=True,
                replay_safety="unsafe",
            ),
            previous_response_id=None,
            conversation_id=None,
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_stream_response_with_retry_retries_before_first_event(monkeypatch) -> None:
    attempts = 0
    rewinds = 0
    failed_attempts: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _connection_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(initial_delay=0.25, jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
            failed_retry_attempts_out=failed_attempts,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert sleeps == [0.25]
    assert failed_attempts == [1]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_keeps_provider_retries_on_first_attempt(
    monkeypatch,
) -> None:
    attempts = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        provider_retry_flags.append(should_disable_provider_managed_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _connection_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )
    ]

    assert provider_retry_flags == [False, True]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_disables_provider_retries_on_first_stateful_provider_hint(
    monkeypatch,
) -> None:
    attempts = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        provider_retry_flags.append(should_disable_provider_managed_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _connection_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.provider_suggested(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                suggested=True,
                replay_safety="safe",
            ),
            previous_response_id="resp_prev",
            conversation_id=None,
        )
    ]

    assert provider_retry_flags == [True, True]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_disables_stateful_provider_retries_with_narrow_policy(
    monkeypatch,
) -> None:
    attempts = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Unrelated policy should not trigger runner rewind")

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        provider_retry_flags.append(should_disable_provider_managed_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            raise _connection_error()
            yield  # pragma: no cover

        return iterator()

    with pytest.raises(APIConnectionError):
        async for _event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.http_status([429]),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        ):
            pass

    assert attempts == 1
    assert provider_retry_flags == [True]


@pytest.mark.asyncio
async def test_stream_response_with_retry_keeps_provider_retries_without_runner_policy(
    monkeypatch,
) -> None:
    attempts = 0
    provider_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        provider_retry_flags.append(should_disable_provider_managed_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=2,
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )
    ]

    assert attempts == 1
    assert provider_retry_flags == [False]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_websocket_pre_event_retries_when_runner_managed(
    monkeypatch,
) -> None:
    calls = 0
    websocket_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        nonlocal calls
        websocket_retry_flags.append(should_disable_websocket_pre_event_retries())
        calls += 1
        if calls == 1:
            raise _connection_error()
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_disable_ws_hidden_retry",
        )

    await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert websocket_retry_flags == [True, True]


@pytest.mark.asyncio
async def test_stream_response_with_retry_keeps_websocket_pre_event_retries_with_unrelated_policy(
    monkeypatch,
) -> None:
    attempts = 0
    websocket_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Unrelated policy should not trigger runner rewind")

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        websocket_retry_flags.append(should_disable_websocket_pre_event_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            raise _connection_error()
            yield  # pragma: no cover

        return iterator()

    with pytest.raises(APIConnectionError):
        async for _event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.http_status([429]),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        ):
            pass

    assert attempts == 1
    assert websocket_retry_flags == [False]


@pytest.mark.asyncio
async def test_stream_response_with_retry_keeps_websocket_pre_event_retries_for_partial_all_policy(
    monkeypatch,
) -> None:
    attempts = 0
    websocket_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        raise AssertionError("Partial all() policy should not trigger runner rewind")

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        websocket_retry_flags.append(should_disable_websocket_pre_event_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            raise _connection_error()
            yield  # pragma: no cover

        return iterator()

    with pytest.raises(APIConnectionError):
        async for _event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.all(
                    retry_policies.network_error(),
                    retry_policies.http_status([500]),
                ),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        ):
            pass

    assert attempts == 1
    assert websocket_retry_flags == [False]


@pytest.mark.asyncio
async def test_get_response_with_retry_disables_websocket_pre_event_retries_when_disabled(
    monkeypatch,
) -> None:
    websocket_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    async def get_response() -> ModelResponse:
        websocket_retry_flags.append(should_disable_websocket_pre_event_retries())
        return ModelResponse(
            output=[get_text_message("ok")],
            usage=Usage(requests=1),
            response_id="resp_disable_ws_hidden_retry_zero",
        )

    await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=0,
            policy=retry_policies.never(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    )

    assert websocket_retry_flags == [True]


@pytest.mark.asyncio
async def test_stream_response_with_retry_does_not_leak_provider_retry_disable_to_consumer(
    monkeypatch,
) -> None:
    attempts = 0
    provider_retry_flags: list[bool] = []
    consumer_retry_flags: list[bool] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        provider_retry_flags.append(should_disable_provider_managed_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _connection_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    async for _event in stream_response_with_retry(
        get_stream=get_stream,
        rewind=rewind,
        retry_settings=ModelRetrySettings(
            max_retries=1,
            policy=retry_policies.network_error(),
        ),
        get_retry_advice=lambda _request: None,
        previous_response_id=None,
        conversation_id=None,
    ):
        consumer_retry_flags.append(should_disable_provider_managed_retries())

    assert provider_retry_flags == [False, True]
    assert consumer_retry_flags == [False]


@pytest.mark.asyncio
async def test_stream_response_with_retry_treats_timeout_error_as_retryable(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        return None

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise TimeoutError("Timed out while waiting for websocket receive.")
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(initial_delay=0.25, jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )
    ]

    assert attempts == 2
    assert sleeps == [0.25]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_allows_stateful_retry_when_provider_marks_safe(
    monkeypatch,
) -> None:
    attempts = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _connection_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(jitter=False),
                policy=retry_policies.provider_suggested(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                suggested=True,
                replay_safety="safe",
            ),
            previous_response_id="resp_prev",
            conversation_id=None,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert sleeps == [0.25]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_allows_stateful_retry_for_http_failure_advice(
    monkeypatch,
) -> None:
    attempts = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _status_error_without_code(500, "server_error")
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(jitter=False),
                policy=retry_policies.provider_suggested(),
            ),
            get_retry_advice=get_openai_retry_advice,
            previous_response_id="resp_prev",
            conversation_id=None,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert sleeps == [0.25]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_allows_custom_policy_to_override_provider_veto(
    monkeypatch,
) -> None:
    attempts = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _status_error_without_code(429, "rate_limit")
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(jitter=False),
                policy=retry_policies.http_status([429]),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(
                suggested=False,
                reason="server veto",
                normalized=ModelRetryNormalizedError(status_code=429),
            ),
            previous_response_id=None,
            conversation_id=None,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert sleeps == [0.25]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_rejects_stateful_retry_without_replay_safety() -> None:
    attempts = 0

    async def rewind() -> None:
        raise AssertionError("Stateful streaming retry should not rewind when replay is vetoed")

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            raise _connection_error()
            yield  # pragma: no cover

        return iterator()

    with pytest.raises(APIConnectionError):
        async for _event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.provider_suggested(),
            ),
            get_retry_advice=lambda _request: ModelRetryAdvice(suggested=True),
            previous_response_id="resp_prev",
            conversation_id=None,
        ):
            pass

    assert attempts == 1


@pytest.mark.asyncio
async def test_stream_response_with_retry_stops_after_retry_budget_exhausted(
    monkeypatch,
) -> None:
    attempts = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            raise _connection_error()
            yield  # pragma: no cover

        return iterator()

    with pytest.raises(APIConnectionError):
        async for _event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(initial_delay=0.25, jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        ):
            pass

    assert attempts == 2
    assert rewinds == 1
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_stream_response_with_retry_retries_after_pre_output_event(monkeypatch) -> None:
    attempts = 0
    rewinds = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                yield cast(TResponseStreamEvent, {"type": "response.created"})
                raise _connection_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})
            yield cast(TResponseStreamEvent, {"type": "response.in_progress"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(initial_delay=0.25, jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert sleeps == [0.25]
    assert events == [
        cast(TResponseStreamEvent, {"type": "response.created"}),
        cast(TResponseStreamEvent, {"type": "response.created"}),
        cast(TResponseStreamEvent, {"type": "response.in_progress"}),
    ]


@pytest.mark.asyncio
async def test_stream_response_with_retry_does_not_retry_after_output_event() -> None:
    attempts = 0

    async def rewind() -> None:
        raise AssertionError("Streaming retries should stop after output has been emitted")

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            yield cast(TResponseStreamEvent, {"type": "response.output_item.added"})
            raise _connection_error()

        return iterator()

    with pytest.raises(APIConnectionError):
        async for _event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        ):
            pass

    assert attempts == 1


@pytest.mark.asyncio
async def test_stream_response_with_retry_closes_abandoned_stream_before_retry(
    monkeypatch,
) -> None:
    rewinds = 0
    sleeps: list[float] = []
    first_stream = _AcloseTrackingStream(error_before_yield=_connection_error())
    second_stream = _AcloseTrackingStream(
        events=[cast(TResponseStreamEvent, {"type": "response.created"})]
    )
    streams = [first_stream, second_stream]

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        return streams.pop(0)

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                backoff=ModelRetryBackoffSettings(initial_delay=0.25, jitter=False),
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        )
    ]

    assert rewinds == 1
    assert sleeps == [0.25]
    assert first_stream.aclose_calls == 1
    assert second_stream.aclose_calls == 1
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_preserves_conversation_locked_compatibility(
    monkeypatch,
) -> None:
    attempts = 0
    rewinds = 0
    failed_attempts: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _conversation_locked_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
            failed_retry_attempts_out=failed_attempts,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert failed_attempts == [1]
    assert sleeps == [1.0]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_disables_provider_retries_on_stateful_compat_replay(
    monkeypatch,
) -> None:
    attempts = 0
    rewinds = 0
    provider_retry_flags: list[bool] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def rewind() -> None:
        nonlocal rewinds
        rewinds += 1

    def get_stream() -> AsyncIterator[TResponseStreamEvent]:
        nonlocal attempts
        provider_retry_flags.append(should_disable_provider_managed_retries())
        attempts += 1

        async def iterator() -> AsyncIterator[TResponseStreamEvent]:
            if attempts == 1:
                raise _conversation_locked_error()
            yield cast(TResponseStreamEvent, {"type": "response.created"})

        return iterator()

    events = [
        event
        async for event in stream_response_with_retry(
            get_stream=get_stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(max_retries=1),
            get_retry_advice=lambda _request: None,
            previous_response_id="resp_prev",
            conversation_id=None,
        )
    ]

    assert attempts == 2
    assert rewinds == 1
    assert provider_retry_flags == [False, True]
    assert sleeps == [1.0]
    assert events == [cast(TResponseStreamEvent, {"type": "response.created"})]


@pytest.mark.asyncio
async def test_stream_response_with_retry_closes_current_stream_when_consumer_stops_early() -> None:
    stream = _CloseTrackingStream(
        events=[
            cast(TResponseStreamEvent, {"type": "response.created"}),
            cast(TResponseStreamEvent, {"type": "response.in_progress"}),
        ]
    )

    async def rewind() -> None:
        raise AssertionError("Early consumer exit should not rewind state")

    outer_stream = cast(
        Any,
        stream_response_with_retry(
            get_stream=lambda: stream,
            rewind=rewind,
            retry_settings=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            ),
            get_retry_advice=lambda _request: None,
            previous_response_id=None,
            conversation_id=None,
        ),
    )

    first_event = await outer_stream.__anext__()
    assert first_event == cast(TResponseStreamEvent, {"type": "response.created"})

    await outer_stream.aclose()

    assert stream.close_calls == 1
