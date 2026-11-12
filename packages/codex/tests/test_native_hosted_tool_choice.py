"""Explicit hosted selectors through actual native HTTP and portable state."""

import pytest

from agents import (
    Agent,
    FileSearch,
    HostedMCP,
    NativeProvider,
    SessionState,
    ToolRef,
    function_tool,
)
from tests.native_fixtures import assistant_output, function_call, responses as responses


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


async def test_native_hosted_choice_switches_to_captured_function(responses):
    effects = []

    @function_tool
    def lookup() -> str:
        """Return the captured lookup result."""
        effects.append("lookup")
        return "found"

    responses.respond(assistant_output("initial"))
    responses.respond(function_call("lookup", {}, "choice-lookup"))
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_fixture"]), lookup],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Start.")).get_result()
            await session.update(model_settings={"tool_choice": ToolRef("lookup")})
            await (await session.submit("Use lookup.")).get_result()
    assert responses.requests[1]["tool_choice"] == "required"
    assert effects == ["lookup"]


@pytest.mark.parametrize("mode", ["auto", "none"])
async def test_native_managed_profile_clears_hosted_choice(responses, mode):
    responses.respond(assistant_output("initial"))
    responses.respond(assistant_output("updated"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        worker = Agent(
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
            name="Worker",
            model="fixture-hosted-choice",
            instructions="Work.",
            provider=provider,
        )
        agent = Agent(
            name="Root",
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
            handoffs=[worker],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Start.")).get_result()
            await session.update(model_settings={"tool_choice": mode})
            await (await session.submit("Use the current mode.")).get_result()
    assert responses.requests[1]["tool_choice"] == mode


async def test_native_mcp_choice_keeps_read_only_filter_without_name_list(responses):
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=[
                HostedMCP(
                    tool_config={
                        "type": "mcp",
                        "server_label": "docs",
                        "server_url": "https://docs.invalid/mcp",
                        "require_approval": "never",
                        "allowed_tools": {"read_only": True},
                    }
                )
            ],
            model_settings={
                "tool_choice": {"type": "mcp", "server_label": "docs", "name": "lookup"}
            },
        )
        async with agent.session() as session:
            await (await session.submit("Choose lookup.")).get_result()
    request = responses.requests[0]
    assert request["tool_choice"] == {"type": "mcp", "server_label": "docs", "name": "lookup"}
    assert next(tool for tool in request["tools"] if tool["type"] == "mcp")["allowed_tools"] == {
        "read_only": True
    }
