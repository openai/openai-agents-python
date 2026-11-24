"""Restricted tool budgets reject provider authority hidden behind prompt definitions."""

import pytest

from agents import Agent, ModelSettings, RunBudget, RunConfig, Runner, UserError
from agents.testing import ScriptedModel
from tests.test_responses import get_function_tool_call


async def execute(agent, budget, streamed):
    config = RunConfig(budget=budget, tracing_disabled=True)
    if not streamed:
        return await Runner.run(agent, "go", run_config=config)
    result = Runner.run_streamed(agent, "go", run_config=config)
    async for _ in result.stream_events():
        pass
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize("surface", ["prompt", "extra_body", "extra_args", "nested_body"])
async def test_restricted_tool_authority_cannot_come_from_opaque_prompt(
    streamed, inherited, surface
):
    model = ScriptedModel()
    kwargs = {"name": "restricted", "model": model}
    if surface == "prompt":
        kwargs["prompt"] = {"id": "opaque-prompt"}
    else:
        overrides = {"prompt": {"id": "opaque-prompt"}}
        kwargs["model_settings"] = ModelSettings(
            **(
                {"extra_args": {"extra_body": overrides}}
                if surface == "nested_body"
                else {surface: overrides}
            )
        )
    restricted = Agent(**kwargs)
    if inherited:
        delegate = restricted.as_tool("ask", "Ask child", failure_error_function=None)
        agent = Agent(
            name="parent",
            model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
            tools=[delegate],
        )
    else:
        agent = restricted
    with pytest.raises(UserError, match="RunBudget.allowed_tools"):
        await execute(agent, RunBudget(allowed_tools=("ask",)), streamed)
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_responses_prompt_without_local_tools_rejected_before_provider(streamed):
    import httpx2
    from openai import AsyncOpenAI

    from agents.models.openai_responses import OpenAIResponsesModel

    requests = []

    async def handler(request):
        requests.append(request)
        raise AssertionError("The opaque prompt must not reach the provider.")

    async with AsyncOpenAI(
        api_key="synthetic-test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        agent = Agent(
            name="restricted",
            model=OpenAIResponsesModel("example", client),
            prompt={"id": "opaque-prompt"},
        )
        with pytest.raises(UserError, match="RunBudget.allowed_tools"):
            await execute(agent, RunBudget(allowed_tools=()), streamed)
    assert not requests
