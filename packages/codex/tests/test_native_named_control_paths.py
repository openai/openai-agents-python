"""Named choices preserve capture, MCP identity and required tool boundaries."""

from __future__ import annotations

from pydantic import BaseModel

from agents import Agent, FileSearch, HttpMCP, NativeProvider, ToolRef, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_mcp_tools import Peer, mcp_call


def provider_for(model):
    return NativeProvider(
        base_url=model.url, api_key="synthetic-named-key", supports_websockets=False
    )


async def test_named_target_and_literal_clear_restore_over_agent_default(responses):
    @function_tool
    def selected() -> str:
        return "selected"

    @function_tool
    def other() -> str:
        return "other"

    for _ in range(4):
        responses.respond(assistant_output("finished"))
    async with timeout(35), provider_for(responses) as provider:
        agent = Agent(
            model="fixture-unlisted-named",
            instructions="Answer.",
            tools=[selected, other],
            provider=provider,
            model_settings={"tool_choice": selected.ref},
        )
        async with agent.session() as session:
            await (await session.submit("capture forced target")).get_result()
            named = await session.save_state()
            await session.update(model_settings={"tool_choice": None})
            await (await session.submit("clear forced target")).get_result()
            cleared = await session.save_state()
        async with agent.session(state=named) as restored:
            await (await restored.submit("restore forced target")).get_result()
        async with agent.session(state=cleared) as restored:
            await (await restored.submit("restore explicit clear")).get_result()
    for index in (0, 2):
        assert responses.requests[index]["tool_choice"] == "required"
        assert "other" not in str(responses.requests[index]["tools"])
    for index in (1, 3):
        assert responses.requests[index]["tool_choice"] == "auto"
        assert "other" in str(responses.requests[index]["tools"])


async def test_named_mcp_leaf_uses_current_admitted_namespace_and_actual_native_call(responses):
    peer = Peer()
    async with timeout(35), peer.serve() as url, provider_for(responses) as provider:
        remote = HttpMCP(name="docs", url=url, tools=["search", "excluded"])
        target = remote.tool("search")
        responses.respond(mcp_call(target.name, target.namespace, "named-mcp-once"))
        responses.respond(assistant_output("finished"))
        agent = Agent(
            model="fixture-unlisted-named",
            instructions="Search.",
            tools=[remote],
            provider=provider,
            model_settings={"tool_choice": target},
        )
        await agent.run("named MCP")
    assert [call["name"] for call in peer.calls] == ["search"]
    for request in responses.requests:
        assert request["tool_choice"] == "required"
        [namespace] = request["tools"]
        assert namespace["name"] == target.namespace
        assert [tool["name"] for tool in namespace["tools"]] == [target.name]


async def test_named_hosted_leaf_uses_one_captured_spec_without_local_authority(responses):
    responses.respond(assistant_output("finished"))
    async with timeout(30), provider_for(responses) as provider:
        await Agent(
            model="fixture-unlisted-named",
            instructions="Search.",
            tools=[FileSearch(vector_store_ids=["synthetic-store"])],
            provider=provider,
            model_settings={"tool_choice": ToolRef("file_search", "functions")},
        ).run("hosted")
    [request] = responses.requests
    assert request["tool_choice"] == "required"
    [spec] = request["tools"]
    assert spec["type"] == "file_search" and spec["vector_store_ids"] == ["synthetic-store"]


class Answer(BaseModel):
    answer: str


async def test_named_leaf_keeps_native_structured_output_validation(responses):
    effects = []

    @function_tool
    def selected() -> str:
        effects.append("selected")
        return "selected"

    responses.respond(function_call("selected", {}, "structured-selected"))
    responses.respond(assistant_output('{"answer":"validated"}'))
    async with timeout(30), provider_for(responses) as provider:
        result = await Agent(
            model="fixture-unlisted-named",
            instructions="Answer.",
            tools=[selected],
            output_type=Answer,
            provider=provider,
            model_settings={"tool_choice": selected.ref},
        ).run("structured")
    assert result.final_output == Answer(answer="validated") and effects == ["selected"]
    assert all(request["text"]["format"]["type"] == "json_schema" for request in responses.requests)


async def test_named_leaf_selects_direct_function_in_mixed_code_mode(responses):
    effects = []

    @function_tool
    def selected() -> str:
        effects.append("selected")
        return "selected result"

    responses.respond(function_call("selected", {}, "mixed-selected"))
    responses.respond(assistant_output("mixed completed"))
    async with timeout(30), provider_for(responses) as provider:
        result = await Agent(
            model="fixture-unlisted-named",
            instructions="Select.",
            tools=[selected],
            provider=provider,
            programmatic_tool_calling=True,
            model_settings={"tool_choice": selected.ref},
        ).run("Select the direct function.")
    assert result.final_output == "mixed completed"
    assert effects == ["selected"]
    assert len(responses.requests) == 2
    for request in responses.requests:
        assert request["tool_choice"] == "required"
        assert [tool["name"] for tool in request["tools"]] == ["selected"]
