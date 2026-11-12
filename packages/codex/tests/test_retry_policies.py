"""Public native retry configuration and compositional policy behavior."""

import inspect
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from agents.retry import (
    ModelRetrySettings,
    NormalizedError,
    RetryAdvice,
    RetryBackoffSettings,
    RetryDecision,
    RetryPolicy,
    RetryPolicyContext,
    retry_policies,
)


async def decide(policy: RetryPolicy, source: RetryPolicyContext) -> bool | RetryDecision:
    result = policy(source)
    return await result if inspect.isawaitable(result) else result


def test_native_settings_capture_policy_separately_from_request_data() -> None:
    from agents import Agent, ModelRetrySettings as PublicSettings
    from agents.session import _native_model_settings

    assert PublicSettings is ModelRetrySettings
    policy = retry_policies.network_error()
    settings = ModelRetrySettings(max_retries=2, policy=policy)
    agent = Agent(
        instructions="Synthetic test.", model_settings={"retry": settings, "attempt_timeout_ms": 11}
    )
    assert agent.model_settings["retry"] is settings
    captured = _native_model_settings(None, agent.model_settings)
    assert captured["request_controls"] == {
        "retry": {"max_retries": 2, "has_policy": True, "configured": True},
        "attempt_timeout_ms": 11,
    }
    assert json.loads(json.dumps(captured))["request_controls"]["retry"]["has_policy"] is True
    assert settings.to_json_dict() == {"max_retries": 2}


@pytest.mark.parametrize("timeout", [True, 0, -1, 2**32])
def test_attempt_timeout_rejected_at_public_capture(timeout: int) -> None:
    from agents import Agent

    with pytest.raises(ValueError, match="attempt_timeout_ms"):
        Agent(instructions="Synthetic test.", model_settings={"attempt_timeout_ms": timeout})


def context(advice: RetryAdvice | None = None) -> RetryPolicyContext:
    return RetryPolicyContext(
        attempt_id="source-turn-attempt-1",
        attempt=1,
        request_attempt=1,
        max_retries=2,
        model="synthetic-model",
        thread_id="source-thread",
        turn_id="source-turn",
        transport="http",
        normalized=NormalizedError(status_code=503, is_network_error=True),
        provider_advice=advice,
    )


def test_settings_json_excludes_runtime_policy_and_preserves_captured_values() -> None:
    policy = retry_policies.network_error()
    settings = ModelRetrySettings(max_retries=2, backoff=RetryBackoffSettings(), policy=policy)
    assert settings.policy is policy
    data = settings.to_json_dict()
    assert json.loads(json.dumps(data)) == {
        "max_retries": 2,
        "backoff": {
            "initial_delay_ms": 250,
            "max_delay_ms": 2_000,
            "multiplier": 2,
            "jitter": True,
        },
    }
    data["max_retries"] = 17
    assert settings.max_retries == 2
    assert ModelRetrySettings().to_json_dict() == {}
    assert ModelRetrySettings(max_retries=0).to_json_dict() == {"max_retries": 0}
    with pytest.raises(FrozenInstanceError):
        settings.max_retries = 17


@pytest.mark.parametrize("value", [True, -1, 0.5, 2**32])
def test_invalid_budget_rejected(value: int) -> None:
    with pytest.raises(ValueError):
        ModelRetrySettings(max_retries=value)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 10**1000])
def test_invalid_delay_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        RetryDecision(True, delay_ms=value)
    with pytest.raises(ValueError):
        RetryBackoffSettings(initial_delay_ms=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("combine", [retry_policies.all, retry_policies.any])
async def test_provider_hard_veto_survives_positive_policy(
    combine: Callable[..., RetryPolicy],
) -> None:
    async def approve(_context: RetryPolicyContext) -> RetryDecision:
        return RetryDecision(True, approve_unsafe_replay=True)

    policy = combine(approve, retry_policies.provider_suggested())
    result = await decide(policy, context(RetryAdvice(suggested=False, replay_safety="safe")))
    assert isinstance(result, RetryDecision) and result.retry is False


@pytest.mark.asyncio
@pytest.mark.parametrize("combine", [retry_policies.all, retry_policies.any])
@pytest.mark.parametrize("explicit", [False, True])
async def test_provider_unsafe_requires_explicit_approval(
    combine: Callable[..., RetryPolicy], explicit: bool
) -> None:
    policy = combine(
        retry_policies.provider_suggested(),
        lambda _context: RetryDecision(True, delay_ms=7, approve_unsafe_replay=explicit),
    )
    result = await decide(policy, context(RetryAdvice(suggested=False, replay_safety="unsafe")))
    assert isinstance(result, RetryDecision) and result.retry is explicit
    if explicit:
        assert result.delay_ms == 7


@pytest.mark.asyncio
async def test_composed_async_policy_keeps_original_context_and_exception() -> None:
    original = RuntimeError("synthetic policy failure")
    source = context()

    async def broken(received: RetryPolicyContext) -> bool:
        assert received is source
        raise original

    policy = retry_policies.any(retry_policies.http_status([503]), broken)
    with pytest.raises(RuntimeError) as caught:
        await decide(policy, source)
    assert caught.value is original
