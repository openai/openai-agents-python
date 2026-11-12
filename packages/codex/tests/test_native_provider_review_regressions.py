"""Provider timeouts and native compaction preserve their existing request owners."""

import asyncio

import pytest

from _agents_codex._model_retry import _normalized_error
from agents import (
    Agent,
    ModelResponse,
    ModelRetrySettings,
    NativeProvider,
    RetryBackoffSettings,
    retry_policies,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.asyncio


async def test_timeout_internal_cancellation_is_not_a_producer_abort():
    try:
        await asyncio.wait_for(asyncio.Event().wait(), timeout=0)
    except asyncio.TimeoutError as error:
        assert isinstance(error.__cause__, asyncio.CancelledError)
        facts = _normalized_error(error)
    assert facts.is_timeout and facts.is_network_error
    assert not facts.is_abort


@pytest.mark.parametrize("wrapped", [False, True])
async def test_real_cancellation_without_timeout_keeps_abort_veto(wrapped):
    error = asyncio.CancelledError()
    if wrapped:
        outer = RuntimeError("wrapped abort")
        outer.__cause__ = error
        error = outer
    assert _normalized_error(error).is_abort


async def test_native_network_policy_retries_a_producer_wait_for_timeout():
    attempts = 0

    class Model:
        async def complete(self, _request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=0)
            return ModelResponse([assistant_output("recovered")["item"]])

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        result = await Agent(
            model="fixture",
            instructions="Answer.",
            provider=provider,
            model_settings={
                "retry": ModelRetrySettings(
                    max_retries=1,
                    backoff=RetryBackoffSettings(initial_delay_ms=0, max_delay_ms=0, jitter=False),
                    policy=retry_policies.network_error(),
                )
            },
        ).run("go")
    assert result.final_output == "recovered"
    assert attempts == 2


@pytest.mark.parametrize("with_transform", [False, True])
async def test_hosted_prompt_local_compaction_omits_only_the_internal_prompt(
    responses, with_transform
):
    for text in ["first", "summary", "last"]:
        responses.respond(assistant_output(text))
    prompt_calls, transforms = [], []

    def source(context):
        prompt_calls.append(context.purpose)
        return {"id": "pmpt_sampling_only"}

    def transform(context):
        transforms.append(context.purpose)

    async with (
        timeout(25),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-compaction", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            hosted_prompt=source,
            transform_context=transform if with_transform else None,
            provider=provider,
        )
        async with agent.session() as session:
            assert (await (await session.submit("first")).get_result()).final_output == "first"
            await session.compact()
            assert (await (await session.submit("last")).get_result()).final_output == "last"
    assert len(responses.requests) == 3
    assert [request.get("prompt") for request in responses.requests] == [
        {"id": "pmpt_sampling_only"},
        None,
        {"id": "pmpt_sampling_only"},
    ]
    assert len(prompt_calls) == 2 and "compaction" not in prompt_calls
    assert transforms.count("compaction") == (1 if with_transform else 0)


@pytest.mark.parametrize(
    "declared,purpose", [(False, "generation"), (True, "generation"), (False, "compaction")]
)
async def test_sampling_hosted_prompt_mismatches_remain_rejected(declared, purpose):
    from agents import NativeError
    from tests.test_native_callback_lifetime import callback_owner

    session, state, _ = callback_owner()
    declaration = Agent(
        instructions="Answer.", hosted_prompt={"id": "pmpt_owned"} if declared else None
    )
    session._agent_for_message = lambda _: declaration
    replies = []
    session._native._reply_context = lambda *args: replies.append(args)
    await session._transform_context_body(
        state,
        {
            "step_id": 1,
            "thread": "thread",
            "instance": None,
            "purpose": purpose,
            "hosted_prompt": not declared,
        },
    )
    assert state.failure.done()
    failure = state.failure.result()
    assert isinstance(failure, NativeError)
    assert "Hosted prompt ownership differs" in str(failure)
    assert len(replies) == 1


async def test_distinct_asyncio_timeout_type_keeps_timeout_semantics(monkeypatch):
    class LegacyAsyncioTimeout(Exception):
        pass

    monkeypatch.setattr(asyncio, "TimeoutError", LegacyAsyncioTimeout)
    error = LegacyAsyncioTimeout()
    error.__cause__ = asyncio.CancelledError()
    facts = _normalized_error(error)
    assert facts.is_timeout and facts.is_network_error
    assert not facts.is_abort
