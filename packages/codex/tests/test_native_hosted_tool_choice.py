"""Explicit hosted selectors through actual native HTTP and portable state."""

import pytest

from agents import Agent, FileSearch, NativeProvider, SessionState, function_tool
from tests.native_fixtures import assistant_output, responses as responses


async def test_native_hosted_choice_updates_and_restores(responses):
    for _ in range(4):
        responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect the captured synthetic declarations.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Select the hosted declaration.")).get_result()
            saved = SessionState.from_json((await session.save_state()).to_json())
            await session.update(model_settings={"tool_choice": "auto"})
            await (await session.submit("Clear the preference.")).get_result()
        async with agent.session(state=saved) as restored:
            await (await restored.submit("Restore the captured selector.")).get_result()
        async with agent.session(state=saved) as restored:
            await restored.update(model_settings={"tool_choice": "auto"})
            await (await restored.submit("Use the explicit current override.")).get_result()
    assert [request["tool_choice"] for request in responses.requests] == [
        {"type": "file_search"},
        "auto",
        {"type": "file_search"},
        "auto",
    ]
    assert any(tool["type"] == "file_search" for tool in responses.requests[0]["tools"])


async def test_native_hosted_choice_refuses_same_name_function(responses):
    effects = []

    @function_tool
    def file_search() -> str:
        """Return a synthetic function result."""
        effects.append("called")
        return "function"

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect the captured synthetic declarations.",
            provider=provider,
            tools=[file_search],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            with pytest.raises(Exception, match="(?i)hosted.*catalog"):
                await (await session.submit("Select.")).get_result()
    assert not responses.requests
    assert not effects
