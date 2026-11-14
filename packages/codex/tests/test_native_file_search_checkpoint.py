"""Distinct File Search identity through a pending tool checkpoint."""

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FileSearch,
    FunctionTool,
    NativeProvider,
    SessionState,
    ToolRef,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_hosted_tools import completed
from tests.test_native_manual_tools import next_request


class Lookup(BaseModel):
    key: str


@pytest.mark.asyncio
@pytest.mark.parametrize("with_hosted", [False, True])
async def test_same_name_file_search_manual_pending_checkpoint(responses, with_hosted):
    lookup = FunctionTool(
        name="file_search",
        description="Find an application-owned record.",
        input_type=Lookup,
        output_type=str,
    )
    hosted = FileSearch(vector_store_ids=["vs_synthetic"], include_search_results=True)
    search = {
        "type": "file_search_call",
        "id": "fs_completed",
        "status": "completed",
        "queries": ["synthetic query"],
        "results": [],
    }
    local_call = function_call("file_search", {"key": "A"}, "local-pending")
    if with_hosted:
        responses.respond(completed(search), local_call)
    else:
        responses.respond(local_call)
    responses.respond(assistant_output("local result accepted"))
    responses.respond(assistant_output("hosted only"))
    async with (
        timeout(25),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        agent = Agent(
            name="Source",
            model="gpt-5.5",
            instructions="Find records.",
            tools=[hosted, lookup],
            provider=provider,
        )
        async with agent.session() as session:
            await session.submit("Find A.", tools=[hosted, lookup] if with_hosted else [lookup])
            request = await next_request(session.interactions(), lookup)
            assert request.call_id == "local-pending"
            assert request.arguments.key == "A"
            saved = SessionState.from_json((await session.suspend()).to_json())
        assert len(responses.requests) == 1
        async with agent.session(state=saved) as session:
            request = await next_request(session.interactions(), lookup)
            assert request.call_id == "local-pending"
            await request.respond("local settlement")
            assert (
                await session.restored_turn.get_result()
            ).final_output == "local result accepted"
            turn = await session.submit("Check hosted search only.", tools=[hosted])
            assert (await turn.get_result()).final_output == "hosted only"
    assert len(responses.requests) == 3
    assert tool_output(responses.requests[1], "local-pending") == "local settlement"
    for request in responses.requests[:2]:
        expected = ["file_search", "function"] if with_hosted else ["function"]
        assert sorted(tool["type"] for tool in request["tools"]) == expected
        assert ("file_search_call.results" in request.get("include", [])) is with_hosted
    if with_hosted:
        [retained] = [
            item for item in responses.requests[1]["input"] if item["type"] == "file_search_call"
        ]
        assert {name: retained[name] for name in ("id", "status", "queries")} == {
            name: search[name] for name in ("id", "status", "queries")
        }
    assert [tool["type"] for tool in responses.requests[2]["tools"]] == ["file_search"]
    assert "file_search_call.results" in responses.requests[2]["include"]


@pytest.mark.asyncio
async def test_explicit_default_namespace_local_file_search_keeps_wire_name(responses):
    @function_tool(name="file_search", namespace="functions")
    def lookup(key: str) -> str:
        """Find an application-owned record."""
        return key

    hosted = FileSearch(vector_store_ids=["vs_synthetic"])
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        agent = Agent(
            name="Source",
            model="gpt-5.5",
            instructions="Find records.",
            tools=[hosted, lookup],
            provider=provider,
            model_settings={"tool_choice": ToolRef("file_search", "functions")},
        )
        assert (await agent.run("Find A.")).final_output == "done"
    [request] = responses.requests
    [tool] = request["tools"]
    assert (tool["type"], tool["name"]) == ("function", "file_search")
    assert request["tool_choice"] == "required"
