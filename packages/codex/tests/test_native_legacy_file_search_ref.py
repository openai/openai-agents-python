"""Resolve legacy File Search refs from the exact active Agent declaration."""

import pytest

from agents import (
    Agent,
    FileSearch,
    NativeError,
    NativeProvider,
    SessionState,
    ToolRef,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


@pytest.mark.parametrize("mode", ["operation", "force", "step", "run_override", "idle"])
async def test_hosted_only_profile_legacy_ref_in_mixed_graph(responses, mode):
    @function_tool
    def file_search() -> str:
        """Look up a destination-owned record."""
        pytest.fail("The source profile must not run the destination callback")

    legacy = ToolRef("file_search", "functions")
    responses.respond(assistant_output("source only"))
    if mode == "idle":
        responses.respond(assistant_output("source only after restore"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        target = Agent(
            name="destination",
            model="gpt-5.5",
            instructions="Search destination.",
            tools=[FileSearch(vector_store_ids=["vs_destination"]), file_search],
        )
        root = Agent(
            name="source",
            model="gpt-5.5",
            instructions="Search source.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_source"])],
            handoffs=[target],
            model_settings={"tool_choice": legacy} if mode == "force" else {},
        )
        async with root.session() as session:
            options = {}
            if mode == "operation":
                options["tools"] = [legacy]
            elif mode == "step":
                options["before_step"] = lambda _, *, step_index: [legacy]
            elif mode == "run_override":
                options["model_settings_override"] = {"tool_choice": legacy}
            elif mode == "idle":
                await session.update(tools=[legacy])
            turn = await session.submit("Read source only.", **options)
            assert (await turn.get_result()).final_output == "source only"
            if mode == "idle":
                saved = SessionState.from_json((await session.save_state()).to_json())
        if mode == "idle":
            async with root.session(state=saved) as reopened:
                turn = await reopened.submit("Read source only after restore.")
                assert (await turn.get_result()).final_output == "source only after restore"
    assert len(responses.requests) == (2 if mode == "idle" else 1)
    for request in responses.requests:
        [tool] = request["tools"]
        assert tool["type"] == "file_search"
        assert tool["vector_store_ids"] == ["vs_source"]
        if mode in ("force", "run_override"):
            assert request["tool_choice"] == "required"


async def test_omitted_local_file_search_cannot_fall_back_to_hosted_after_handoff(responses):
    @function_tool
    def file_search() -> str:
        """Look up a destination-owned record."""
        pytest.fail("A narrowed local callback must remain excluded")

    hosted = FileSearch(vector_store_ids=["vs_destination"])
    responses.respond(function_call("transfer_to_destination", {}, "transfer"))
    responses.respond(assistant_output("destination"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        target = Agent(
            name="destination",
            model="gpt-5.5",
            instructions="Search destination.",
            tools=[hosted, file_search],
        )
        root = Agent(
            name="source",
            model="gpt-5.5",
            instructions="Transfer.",
            provider=provider,
            handoffs=[target],
        )
        async with root.session() as session:
            turn = await session.submit("Transfer.")
            assert (await turn.get_result()).final_output == "destination"
            assert await session.get_active_agent() is target
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with root.session(state=saved) as reopened:
            await reopened.update(
                model_settings={"tool_choice": ToolRef("file_search", "functions")}
            )
            turn = await reopened.submit("Do not run.", tools=[hosted])
            with pytest.raises(NativeError, match="outside the already admitted step catalog"):
                await turn.get_result()
    assert len(responses.requests) == 2
