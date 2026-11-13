"""SDK-managed failures count even when a producer never reports token usage."""

from __future__ import annotations

import pytest

from agents import (
    Agent,
    ModelResponse,
    ModelRetrySettings,
    NativeProvider,
    RetryAdvice,
    RetryDecision,
    SessionState,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output


@pytest.mark.parametrize("measured_tokens", [None, 0, 3])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("retry", [False, True])
async def test_provider_attempt_keeps_measured_or_unknown_usage(stream, retry, measured_tokens):
    original = RuntimeError("failure before first content")
    attempts = []
    decisions = []
    policy_entries = []

    async def policy(context):
        decisions.append(context)
        history = [item async for item in session.turns()]
        policy_entries.append(history[-1]["request_usage_entries"])
        return RetryDecision(retry, delay_ms=0)

    class Model:
        def complete(self, request):
            attempts.append(request)
            if len(attempts) == 1:
                if measured_tokens is not None:
                    assert request.report_usage(
                        {"input_tokens": measured_tokens, "total_tokens": measured_tokens}
                    )
                raise original
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "recovered"}],
                    }
                ],
                {"input_tokens": 7, "total_tokens": 7},
            )

        def get_retry_advice(self, _request):
            return RetryAdvice(suggested=True, replay_safety="safe", response_started=False)

    model = Model()
    if stream:

        async def produce(request):
            attempts.append(request)
            if len(attempts) == 1:
                if measured_tokens is not None:
                    assert request.report_usage(
                        {"input_tokens": measured_tokens, "total_tokens": measured_tokens}
                    )
                raise original
            yield {"type": "response.created", "response": {"id": "resp_recovered"}}
            yield assistant_output("recovered")
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered",
                    "usage": {"input_tokens": 7, "output_tokens": 0, "total_tokens": 7},
                },
            }

        model.stream = produce

    async with timeout(20), NativeProvider(model_backend=model) as provider:
        agent = Agent(
            model="custom-model",
            provider=provider,
            instructions="Answer.",
            model_settings={"retry": ModelRetrySettings(max_retries=1, policy=policy)},
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            if retry:
                result = await turn.get_result()
                assert result.final_output == "recovered"
            else:
                with pytest.raises(RuntimeError) as caught:
                    await turn.get_result()
                assert caught.value is original
            usage = turn.run_usage
            assert usage.requests == (2 if retry else 1)
            assert (
                usage.input_tokens
                == usage.total_tokens
                == (measured_tokens or 0) + (7 if retry else 0)
            )
            assert usage.missing_usage_requests == (1 if measured_tokens is None else 0)
            recorded = [item async for item in session.turns()]
            detail = next(item for item in recorded if item["id"] == turn.turn_id)
            entries = detail["request_usage_entries"]
            assert detail["request_usage_entries_complete"] is True
            assert [entry["attempt_id"] for entry in entries] == [
                request.attempt_id for request in attempts
            ]
            assert [entry["request_ordinal"] for entry in entries] == list(
                range(1, len(attempts) + 1)
            )
            assert all(entry["scope"] == "sdk_model_attempt" for entry in entries)
            assert all(entry["turn_id"] == turn.turn_id for entry in entries)
            assert all(entry["thread_id"] == session.id for entry in entries)
            assert entries[0]["token_usage_known"] is (measured_tokens is not None)
            assert (
                entries[0]["input_tokens"] == entries[0]["total_tokens"] == (measured_tokens or 0)
            )
            assert entries[0]["output_tokens"] == 0
            assert entries[0]["input_tokens_details"] == {
                "cached_tokens": 0,
                "cache_write_tokens": 0,
            }
            assert entries[0]["output_tokens_details"] == {"reasoning_tokens": 0}
            if retry:
                assert entries[1]["token_usage_known"] is True
                assert entries[1]["input_tokens"] == entries[1]["total_tokens"] == 7
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with agent.session(state=saved) as restored:
            restored_detail = next(
                item
                for item in [item async for item in restored.turns()]
                if item["id"] == turn.turn_id
            )
            assert restored_detail["request_usage_entries"] == entries
            assert restored_detail["request_usage_entries_complete"] is True
    assert len(policy_entries) == 1
    assert len(policy_entries[0]) == 1
    assert policy_entries[0][0]["attempt_id"] == attempts[0].attempt_id
    assert policy_entries[0][0]["total_tokens"] == (measured_tokens or 0)
    assert policy_entries[0][0]["token_usage_known"] is (measured_tokens is not None)
    assert len(attempts) == (2 if retry else 1)
    assert len(decisions) == 1


async def test_opaque_internal_retries_do_not_invent_sdk_attempts():
    attempts = []
    opaque_attempts = []

    class Model:
        def complete(self, request):
            attempts.append(request)
            assert not request.retry_configured
            for _ in range(3):
                opaque_attempts.append(True)
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "opaque result"}],
                    }
                ],
                {"input_tokens": 7, "total_tokens": 7},
            )

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(model="custom-model", provider=provider, instructions="Answer.")
        async with agent.session() as session:
            turn = await session.submit("go")
            result = await turn.get_result()
            detail = [item async for item in session.turns()][-1]
            assert detail["request_usage_entries_complete"] is True
            entries = detail["request_usage_entries"]
            assert len(entries) == 1
            assert entries[0]["scope"] == "sdk_model_attempt"
            assert entries[0]["attempt_id"] == attempts[0].attempt_id
            assert entries[0]["token_usage_known"] is True
            assert entries[0]["input_tokens"] == 7
    assert len(attempts) == 1 and len(opaque_attempts) == 3
    assert result.run_usage.requests == 1
    assert result.run_usage.input_tokens == 7
