"""Provider File Search and local functions retain distinct admitted identities."""

import pytest
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses

from agents import Agent, FileSearch, NativeProvider, SessionState, ToolRef, function_tool


def kinds(request):
    return sorted(
        tool["type"]
        for tool in request["tools"]
        if tool.get("name") == "file_search" or tool["type"] == "file_search"
    )


@pytest.mark.parametrize(
    "selection", ["auto", "hosted", "local", "none", "hosted-only", "local-only"]
)
async def test_native_same_name_local_and_hosted_file_search(responses, selection):
    effects = []

    @function_tool
    def file_search() -> str:
        """Look up an application-owned record."""
        effects.append("local")
        return "local output"

    hosted = FileSearch(vector_store_ids=["vs_synthetic"], include_search_results=True)
    settings = {}
    submit = {}
    if selection == "hosted":
        settings["tool_choice"] = {"type": "file_search"}
    elif selection == "local":
        settings["tool_choice"] = ToolRef("file_search")
    elif selection == "none":
        submit["tools"] = []
    elif selection == "hosted-only":
        submit["tools"] = [hosted]
    elif selection == "local-only":
        submit["tools"] = [file_search]

    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        agent = Agent(
            name="Source",
            model="gpt-5.5",
            instructions="Use both.",
            tools=[hosted, file_search],
            provider=provider,
            model_settings=settings,
        )
        if selection == "auto":
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "file_search_call",
                        "id": "fs_1",
                        "status": "completed",
                        "queries": ["q"],
                        "results": [],
                    },
                },
                function_call("file_search", {}, "call-local"),
            )
        responses.respond(assistant_output("done"))
        async with agent.session() as session:
            turn = await session.submit("Reply.", **submit)
            assert (await turn.get_result()).final_output == "done"

    expected = {
        "auto": ["file_search", "function"],
        "hosted": ["file_search", "function"],
        "local": ["function"],
        "none": [],
        "hosted-only": ["file_search"],
        "local-only": ["function"],
    }[selection]
    assert len(responses.requests) == (2 if selection == "auto" else 1)
    assert all(kinds(request) == expected for request in responses.requests)
    assert effects == (["local"] if selection == "auto" else [])
    for request in responses.requests:
        assert ("file_search_call.results" in request.get("include", [])) == (
            "file_search" in expected
        )
    if selection == "hosted":
        assert responses.requests[0]["tool_choice"] == {"type": "file_search"}
    elif selection == "local":
        assert responses.requests[0]["tool_choice"] == "required"
    elif selection == "auto":
        assert any(
            row.get("call_id") == "call-local" and row.get("output") == "local output"
            for row in responses.requests[1]["input"]
        )


@pytest.mark.parametrize("hosted_default", [False, True])
async def test_same_name_file_search_idle_default_cold_restore(responses, hosted_default):
    @function_tool
    def file_search() -> str:
        """Look up an application-owned record."""
        return "local output"

    hosted = FileSearch(vector_store_ids=["vs_synthetic"])
    selected = hosted if hosted_default else file_search
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        agent = Agent(
            name="Source",
            model="gpt-5.5",
            instructions="Read.",
            tools=[hosted, file_search],
            provider=provider,
        )
        responses.respond(assistant_output("before"))
        responses.respond(assistant_output("after"))
        async with agent.session() as session:
            await session.update(tools=[selected])
            assert (await (await session.submit("Before.")).get_result()).final_output == "before"
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with agent.session(state=saved) as reopened:
            assert (await (await reopened.submit("After.")).get_result()).final_output == "after"
    expected = ["file_search"] if hosted_default else ["function"]
    assert [kinds(request) for request in responses.requests] == [expected, expected]


async def test_hidden_file_search_identity_cannot_replace_a_hosted_declaration(responses):
    @function_tool(name="file_search", namespace="_agents_sdk_hosted")
    def forged() -> str:
        """This tool must never execute."""
        pytest.fail("A forged hosted selector became callable")

    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(name="Source", model="gpt-5.5", instructions="Read.", provider=provider)
        with pytest.raises(ValueError, match="reserved File Search selector"):
            await Agent(
                name="Forged",
                model="gpt-5.5",
                instructions="Read.",
                provider=provider,
                tools=[forged],
            ).run("Refuse.")
        async with agent.session() as session:
            with pytest.raises(ValueError, match="must have been allowed"):
                await session.submit(
                    "Refuse.", tools=[ToolRef("file_search", "_agents_sdk_hosted")]
                )
    assert not responses.requests


async def test_same_name_file_search_handoff_and_cold_explicit_selection(responses):
    @function_tool
    def file_search() -> str:
        """Look up a record available only to the destination."""
        pytest.fail("The provider did not request a local execution")

    source_search = FileSearch(vector_store_ids=["vs_source"])
    destination_search = FileSearch(
        vector_store_ids=["vs_destination"], include_search_results=True
    )
    responses.respond(function_call("transfer_to_destination", {}, "handoff"))
    responses.respond(assistant_output("destination"))
    responses.respond(assistant_output("local only"))
    responses.respond(assistant_output("hosted only"))
    async with (
        timeout(25),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        target = Agent(
            name="destination",
            model="gpt-5.5",
            instructions="Use destination tools.",
            tools=[destination_search, file_search],
        )
        root = Agent(
            name="source",
            model="gpt-5.5",
            instructions="Use source tools.",
            tools=[source_search],
            handoffs=[target],
            provider=provider,
        )
        async with root.session() as session:
            assert (
                await (await session.submit("Transfer.")).get_result()
            ).final_output == "destination"
            assert await session.get_active_agent() is target
            saved = (await session.save_state()).to_json()
        for selected, output in [(file_search, "local only"), (destination_search, "hosted only")]:
            async with root.session(state=SessionState.from_json(saved)) as reopened:
                assert await reopened.get_active_agent() is target
                turn = await reopened.submit("Read.", tools=[selected])
                assert (await turn.get_result()).final_output == output
    assert [kinds(request) for request in responses.requests] == [
        ["file_search"],
        ["file_search", "function"],
        ["function"],
        ["file_search"],
    ]
    for index, store in [(0, "vs_source"), (1, "vs_destination"), (3, "vs_destination")]:
        [search] = [
            tool for tool in responses.requests[index]["tools"] if tool["type"] == "file_search"
        ]
        assert search["vector_store_ids"] == [store]
        assert ("file_search_call.results" in responses.requests[index].get("include", [])) is (
            index != 0
        )
