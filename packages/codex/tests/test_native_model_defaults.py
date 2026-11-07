"""Keep model reasoning defaults and explicit overrides stable across selection."""

from __future__ import annotations

from typing import Literal

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
)

# Sol model references checked on 2026-11-04:
# https://developers.openai.com/api/docs/models/gpt-6.1-sol
# https://developers.openai.com/api/docs/models/gpt-5.6-sol
MODELS = ("gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-sol")
UNKNOWN_MODEL = "fixture-model-without-context-metadata"


def provider_for(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-model-defaults-key")


@pytest.mark.parametrize("model", MODELS)
async def test_omitted_reasoning_uses_sdk_catalog_defaults(
    responses: ResponsesFixture, model: str
) -> None:
    responses.respond(assistant_output("finished"))
    async with timeout(30), provider_for(responses) as provider:
        result = await Agent(model=model, instructions="Answer.", provider=provider).run("one")
    assert result.final_output == "finished"
    assert result.thread_usage is not None
    assert result.thread_usage.model_context_window == 258_400
    assert len(responses.requests) == 1
    assert responses.requests[0]["model"] == model
    assert responses.requests[0]["reasoning"]["effort"] == "medium"


@pytest.mark.parametrize("effort", ["low", "high"])
async def test_astra_explicit_effort_overrides_catalog_default(
    responses: ResponsesFixture, effort: Literal["low", "high"]
) -> None:
    responses.respond(assistant_output("finished"))
    async with timeout(30), provider_for(responses) as provider:
        result = await Agent(
            model="gpt-6-astra",
            instructions="Answer.",
            provider=provider,
            model_settings={"reasoning_effort": effort},
        ).run("one")
    assert result.final_output == "finished"
    assert result.thread_usage is not None and result.thread_usage.model_context_window == 258_400
    assert len(responses.requests) == 1
    assert responses.requests[0]["reasoning"]["effort"] == effort


@pytest.mark.parametrize("model", MODELS)
async def test_explicit_reasoning_wins_after_model_change(
    responses: ResponsesFixture, model: str
) -> None:
    other = next(candidate for candidate in MODELS if candidate != model)
    for _ in range(3):
        responses.respond(assistant_output("finished"))
    async with timeout(30), provider_for(responses) as provider:
        agent = Agent(
            model=model,
            instructions="Answer.",
            provider=provider,
            model_settings={"reasoning_effort": "low"},
        )
        async with agent.session(model_settings={"reasoning_effort": "high"}) as session:
            assert (
                await (await session.submit("session override")).get_result()
            ).final_output == "finished"
            await session.update(model=other, model_settings={"reasoning_effort": "low"})
            assert (
                await (await session.submit("update override")).get_result()
            ).final_output == "finished"
            await session.update(model=model)
            assert (
                await (await session.submit("retain override")).get_result()
            ).final_output == "finished"
    assert [request["model"] for request in responses.requests] == [model, other, model]
    assert [request["reasoning"]["effort"] for request in responses.requests] == [
        "high",
        "low",
        "low",
    ]
    assert agent.model_settings["reasoning_effort"] == "low"


async def test_omitted_reasoning_reselects_default_after_model_change(
    responses: ResponsesFixture,
) -> None:
    selections = (MODELS[0], UNKNOWN_MODEL, *MODELS[1:], MODELS[0])
    for _ in selections:
        responses.respond(assistant_output("finished"))
    async with timeout(30), provider_for(responses) as provider:
        agent = Agent(model=selections[0], instructions="Answer.", provider=provider)
        async with agent.session() as session:
            for index, model in enumerate(selections):
                await session.update(model=model)
                result = await (await session.submit("selected model")).get_result()
                assert result.final_output == "finished"
                assert result.thread_usage is not None
                assert result.thread_usage.model_context_window == (
                    None if model == UNKNOWN_MODEL else 258_400
                )
                assert responses.requests[index]["model"] == model
    # An unknown model has no catalog effort. Returning to Astra must resolve
    # its default again, without carrying the unknown model's missing metadata.
    assert [request.get("reasoning", {}).get("effort") for request in responses.requests] == [
        "medium",
        None,
        "medium",
        "medium",
        "medium",
    ]
