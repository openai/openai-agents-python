"""Model completion runs on the captured SDK owner before direct effects."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, ModelCompletionContext, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent_observation import provider


@pytest.mark.parametrize("behavior", ["accept", "raise", "cancel"])
async def test_after_model_gates_direct_tools_and_preserves_the_response(responses, behavior):
    responses.respond(function_call("echo", {"value": "hello"}, "echo-1"))
    responses.respond(assistant_output("Done."))
    native = provider(responses)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []
    completions = []
    application = object()
    failure = ValueError("application declined the sampled response")

    @function_tool
    async def echo(value: str) -> str:
        effects.append(value)
        return value

    async def after_model(event: ModelCompletionContext[object]) -> None:
        assert event.context is application
        completions.append(event)
        if len(completions) == 1:
            entered.set()
            await release.wait()
            if behavior == "raise":
                raise failure

    try:
        agent = Agent(
            instructions="Use echo and answer.",
            tools=[echo],
            provider=native,
            model="gpt-5.5",
            after_model=after_model,
        )
        async with agent.session(context=application) as session:
            turn = await session.submit("Begin.")
            waiting = asyncio.create_task(turn.get_result())
            async with timeout(5):
                await entered.wait()
            assert len(responses.requests) == 1
            assert not effects
            assert completions[0].session_id == session.id
            assert completions[0].turn_id == turn.turn_id
            assert completions[0].response_id
            assert completions[0].items[0]["call_id"] == "echo-1"
            if behavior == "cancel":
                await turn.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                assert not effects
            elif behavior == "raise":
                release.set()
                with pytest.raises(ValueError) as caught:
                    await waiting
                assert caught.value is failure
                assert not effects
            else:
                release.set()
                assert (await waiting).final_output == "Done."
                assert effects == ["hello"]
                assert len(completions) == 2
                assert completions[1].response_id != completions[0].response_id
                assert completions[1].items[0]["type"] == "message"
    finally:
        release.set()
        await native.aclose()


async def test_incomplete_model_response_never_starts_a_tool(responses):
    from agents import NativeError

    responses.respond(
        function_call("echo", {"value": "never"}, "unaccepted"),
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "provider refused after proposing the call",
                }
            },
        },
    )
    responses.respond(assistant_output("Recovered."))
    native = provider(responses)
    effects, completions = [], []

    @function_tool
    async def echo(value: str) -> str:
        effects.append(value)
        return value

    async def after_model(event: ModelCompletionContext[object]) -> None:
        completions.append(event.response_id)

    try:
        agent = Agent(
            instructions="Use echo when requested.",
            tools=[echo],
            provider=native,
            model="gpt-5.5",
            after_model=after_model,
        )
        async with timeout(15), agent.session() as session:
            with pytest.raises(NativeError, match="provider refused after proposing the call"):
                await (await session.submit("Try a call.")).get_result()
            assert not effects
            assert not completions
            assert (
                await (await session.submit("Just answer.")).get_result()
            ).final_output == "Recovered."
            assert completions == ["resp_2"]
            assert not effects
    finally:
        await native.aclose()


async def test_after_model_selects_the_current_handoff_profile(responses):
    responses.respond(function_call("transfer_to_destination", {}, "handoff"))
    responses.respond(assistant_output("Destination."))
    responses.respond(assistant_output("Reopened."))
    observed = []

    async def source(event: ModelCompletionContext[object]) -> None:
        observed.append(("source", event.response_id))

    async def destination(event: ModelCompletionContext[object]) -> None:
        observed.append(("destination", event.response_id))

    async with provider(responses) as native:
        target = Agent(name="destination", instructions="Finish the turn.", after_model=destination)
        agent = Agent(
            name="source",
            instructions="Transfer.",
            provider=native,
            model="gpt-5.5",
            handoffs=[target],
            after_model=source,
        )
        async with timeout(15), agent.session() as session:
            assert (await (await session.submit("Transfer.")).get_result()).final_output == (
                "Destination."
            )
            state = await session.save_state()
        async with timeout(15), agent.session(state=state) as restored:
            assert (await (await restored.submit("Continue.")).get_result()).final_output == (
                "Reopened."
            )
    assert observed == [
        ("source", "resp_1"),
        ("destination", "resp_2"),
        ("destination", "resp_3"),
    ]
