"""Public finite model scripts execute through real native Sessions."""

from __future__ import annotations

import pytest

from agents import (
    Agent,
    ModelResponse,
    ModelRetrySettings,
    NativeProvider,
    RetryAdvice,
    RetryBackoffSettings,
)
from agents.testing import ModelError, ScriptedModel


def response(value: str) -> ModelResponse:
    return ModelResponse(
        [
            {
                "type": "message",
                "role": "assistant",
                "id": "msg_scripted",
                "content": [{"type": "output_text", "text": value, "annotations": []}],
            }
        ],
        {"input_tokens": 3, "output_tokens": 0, "total_tokens": 3},
    )


async def test_public_scripted_model_returns_native_result_and_usage():
    model = ScriptedModel([response("scripted native result")])
    async with NativeProvider(model_backend=model) as provider:
        result = await Agent(instructions="Answer.", model="custom-model", provider=provider).run(
            "go"
        )
    assert result.final_output == "scripted native result"
    assert result.run_usage.input_tokens == 3
    model.assert_complete()


async def test_public_scripted_model_keeps_exact_provider_error():
    error = RuntimeError("synthetic exact failure")
    model = ScriptedModel([ModelError(error)])
    async with NativeProvider(model_backend=model) as provider:
        with pytest.raises(RuntimeError) as caught:
            await Agent(instructions="Answer.", model="custom-model", provider=provider).run("go")
    assert caught.value is error
    model.assert_complete()


async def test_public_scripted_model_exhaustion_is_a_fixture_failure():
    model = ScriptedModel([response("first result")])
    with pytest.raises(AssertionError, match="Unconsumed"):
        model.assert_complete()
    async with NativeProvider(model_backend=model) as provider:
        agent = Agent(instructions="Answer.", model="custom-model", provider=provider)
        assert (await agent.run("first")).final_output == "first result"
        with pytest.raises(AssertionError, match="Unexpected scripted model call"):
            await agent.run("extra")
    model.assert_complete()


async def test_public_scripted_model_correlates_native_advice_and_retry():
    error = RuntimeError("synthetic retry failure")
    decisions = []

    def policy(context):
        assert context.error is error
        assert context.provider_advice.reason == "scripted advice"
        decisions.append(context)
        return True

    model = ScriptedModel(
        [
            ModelError(
                error, RetryAdvice(suggested=True, replay_safety="safe", reason="scripted advice")
            ),
            response("retried result"),
        ]
    )
    async with NativeProvider(model_backend=model) as provider:
        result = await Agent(
            instructions="Answer.",
            model="custom-model",
            provider=provider,
            model_settings={
                "retry": ModelRetrySettings(
                    max_retries=1,
                    policy=policy,
                    backoff=RetryBackoffSettings(initial_delay_ms=0, max_delay_ms=0, jitter=False),
                )
            },
        ).run("go")
    assert result.final_output == "retried result"
    assert len(decisions) == 1
    assert result.run_usage.requests == 2
    model.assert_complete()
