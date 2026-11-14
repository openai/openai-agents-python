"""A runtime handoff inherits only the admitted source's provider tools."""

import pytest
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_decision_handoff import Decisions

from agents import Agent, DecisionHandoff, FileSearch, NativeProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("source_has_search", [False, True], ids=["revoke", "inherit"])
async def test_runtime_factory_uses_the_current_hosted_scope(responses, source_has_search):
    created = []

    def search(store):
        return FileSearch(vector_store_ids=[store], include_search_results=True)

    def factory(_context, _input):
        target = Agent(name="runtime", model="gpt-5.5", instructions="Runtime instructions.")
        created.append(target)
        return target

    async with (
        timeout(25),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        source = Agent(
            name="source",
            model="gpt-5.5",
            instructions="Source instructions.",
            tools=[search("vs_source")] if source_has_search else [],
            decisions=Decisions("runtime"),
            handoffs=[
                DecisionHandoff(
                    name="route",
                    description="Choose the runtime Agent.",
                    question="Choose.",
                    targets={"runtime": factory},
                )
            ],
        )
        root = Agent(
            name="root",
            model="gpt-5.5",
            instructions="Root instructions.",
            tools=[] if source_has_search else [search("vs_root")],
            provider=provider,
            handoffs=[source],
        )
        responses.respond(function_call("transfer_to_source", {}, "source-call"))
        responses.respond(function_call("route", {"input": "Next."}, "runtime-call"))
        responses.respond(assistant_output("at runtime"))
        async with root.session() as session:
            assert (await (await session.submit("Go.")).get_result()).final_output == "at runtime"
            assert len(created) == 1
            assert await session.get_active_agent() is created[0]

    assert len(responses.requests) == 3
    expected = [None, "vs_source", "vs_source"] if source_has_search else ["vs_root", None, None]
    for request, store in zip(responses.requests, expected, strict=True):
        tools = [tool for tool in request["tools"] if tool["type"] == "file_search"]
        if store is None:
            assert tools == []
            assert "file_search_call.results" not in request.get("include", [])
        else:
            [tool] = tools
            assert tool["vector_store_ids"] == [store]
            assert "file_search_call.results" in request.get("include", [])
