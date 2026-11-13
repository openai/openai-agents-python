"""Provider observations cross the public model/native retry boundary unchanged."""

import pytest

from agents import (
    Agent,
    ModelResponse,
    ModelRetrySettings,
    NativeProvider,
    RetryAdvice,
    RetryBackoffSettings,
    RetryDecision,
)
from tests._asyncio import timeout


@pytest.mark.parametrize(
    ("response_started", "replay_safety"),
    [(None, "safe"), (False, "safe"), (False, "unsafe")],
)
async def test_external_model_retries_only_with_explicit_safe_response_evidence(
    response_started, replay_safety
):
    original = RuntimeError("synthetic external model failure")
    attempts = []
    seen = []

    class Model:
        def complete(self, request):
            attempts.append(request)
            if len(attempts) == 1:
                raise original
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "recovered"}],
                    }
                ]
            )

        def get_retry_advice(self, _request):
            return RetryAdvice(
                suggested=True, replay_safety=replay_safety, response_started=response_started
            )

    def policy(context):
        seen.append(context)
        return RetryDecision(
            retry=not context.response_started and context.provider_advice.response_started is False
        )

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(
            instructions="Answer.",
            model="custom-model",
            provider=provider,
            model_settings={
                "retry": ModelRetrySettings(
                    max_retries=1,
                    policy=policy,
                    backoff=RetryBackoffSettings(initial_delay_ms=0, max_delay_ms=0),
                )
            },
        )
        if response_started is False and replay_safety == "safe":
            assert (await agent.run("Reply.")).final_output == "recovered"
            assert len(attempts) == 2
        else:
            with pytest.raises(RuntimeError) as failure:
                await agent.run("Reply.")
            assert failure.value is original
            assert len(attempts) == 1
    assert len(seen) == 1
    assert seen[0].response_started is False
    assert seen[0].provider_advice.response_started is response_started
