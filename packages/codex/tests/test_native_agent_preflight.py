"""One-shot admission follows the captured active profile and operation selection."""

import pytest

from agents import (
    Agent,
    CapabilityError,
    FunctionTool,
    InteractionRequired,
    NativeProvider,
    RunBudget,
    ToolRef,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import function_declarations
from tests.test_native_manual_tools import LookupArgs


@pytest.mark.parametrize(
    ("kind", "budget"),
    [
        ("automatic", None),
        ("manual", None),
        ("managed", None),
        pytest.param("managed", RunBudget(max_requests=2), id="managed-request-budget"),
    ],
)
async def test_run_restores_target_default_selection_with_current_bindings(responses, kind, budget):
    calls = []

    @function_tool(name="lookup", needs_approval=kind == "managed")
    def automatic(key: str) -> str:
        calls.append(key)
        return "current reply"

    lookup = (
        FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
        if kind == "manual"
        else automatic
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-profile-preflight"
    ) as provider:
        target = Agent(name="B", instructions="Read B.", tools=[lookup])
        source = Agent(name="A", instructions="Route to B.", handoffs=[target], provider=provider)
        async with source.session() as session:
            await session.update(agent=target)
            await session.update(tools=[lookup.ref])
            saved = await session.save_state()
        responses.respond(function_call("lookup", {"key": "B"}, "lookup-B"))
        responses.respond(assistant_output("B completed"))
        async with timeout(20):
            if kind != "automatic":
                with pytest.raises(InteractionRequired) as pending:
                    await source.run("Read B.", state=saved, budget=budget)
                assert pending.value.state is not None
                assert calls == []
                result = await source.run(
                    None,
                    state=pending.value.state,
                    budget=budget,
                    on_interaction=lambda request: request.reply(
                        True if kind == "managed" else "current reply"
                    ),
                )
            else:
                result = await source.run("Read B.", state=saved, budget=budget)
        assert result.final_output == "B completed"
        assert calls == ([] if kind == "manual" else ["B"])
        assert len(responses.requests) == 2


async def test_token_budgeted_restore_rejects_captured_active_profile_managed_approval(responses):
    calls = []

    @function_tool(name="lookup", needs_approval=True)
    def managed(key: str) -> str:
        calls.append(key)
        return "current reply"

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-restored-managed-budget"
    ) as provider:
        target = Agent(name="B", instructions="Read B.", tools=[managed])
        source = Agent(name="A", instructions="Route to B.", handoffs=[target], provider=provider)
        async with source.session() as session:
            await session.update(agent=target)
            await session.update(tools=[managed.ref])
            saved = await session.save_state()
        async with timeout(20):
            with pytest.raises(CapabilityError, match="cannot be captured"):
                await source.run(
                    "Read B.", state=saved, budget=RunBudget(max_requests=5, max_tokens=1000)
                )
        assert responses.requests == []
        assert calls == []


async def test_restored_target_can_checkpoint_original_root_after_handoff(responses):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with NativeProvider(base_url=responses.url, api_key="synthetic-cycle") as provider:
        root = Agent(
            name="A",
            instructions="Read A.",
            tools=[lookup],
            handoffs=[lambda: middle],
            provider=provider,
        )
        middle = Agent(name="B", instructions="Return to A.", handoffs=[root])
        async with root.session() as session:
            await session.update(agent=middle)
            saved = await session.save_state()
        responses.respond(function_call("transfer_to_a", {}, "handoff-A"))
        responses.respond(function_call("lookup", {"key": "A"}, "lookup-A"))
        async with timeout(20):
            with pytest.raises(InteractionRequired) as pending:
                await root.run("Read A.", state=saved)
            assert len(responses.requests) == 2
            assert pending.value.state is not None
            responses.respond(assistant_output("A completed"))
            result = await root.run(
                None,
                state=pending.value.state,
                on_interaction=lambda request: request.reply("current reply"),
            )
            assert result.final_output == "A completed"


async def test_budgeted_restore_resolves_explicit_tool_against_active_profile(responses):
    calls = []

    @function_tool(name="lookup")
    def automatic(key: str) -> str:
        calls.append(key)
        return "current reply"

    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-explicit-restored-budget"
    ) as provider:
        target = Agent(name="B", instructions="Read B.", tools=[automatic])
        source = Agent(
            name="A",
            instructions="Route to B.",
            tools=[manual],
            handoffs=[target],
            provider=provider,
        )
        async with source.session() as session:
            await session.update(agent=target)
            saved = await session.save_state()
        responses.respond(function_call("lookup", {"key": "B"}, "lookup-B"))
        responses.respond(assistant_output("B completed"))
        async with timeout(20):
            result = await source.run(
                "Read B.", state=saved, tools=[ToolRef("lookup")], budget=RunBudget(max_requests=5)
            )
        assert result.final_output == "B completed"
        assert calls == ["B"]


@pytest.mark.parametrize("selection", [None, [], [ToolRef("transfer_to_b")]])
async def test_budget_preflight_follows_static_handoff_operation_ceiling(responses, selection):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-handoff-budget"
    ) as provider:
        target = Agent(name="B", instructions="Read B.", tools=[lookup])
        source = Agent(name="A", instructions="Route to B.", handoffs=[target], provider=provider)
        async with timeout(20):
            if selection is None:
                with pytest.raises(CapabilityError, match="cannot be captured"):
                    await source.run("Read B.", budget=RunBudget(max_requests=5, max_tokens=1000))
                assert not responses.requests
            else:
                if selection:
                    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
                responses.respond(assistant_output("No manual tools"))
                result = await source.run(
                    "Read B.", tools=selection, budget=RunBudget(max_requests=5)
                )
                assert result.final_output == "No manual tools"
                assert len(responses.requests) == (2 if selection else 1)
                assert not function_declarations(responses.requests[-1])


@pytest.mark.parametrize("select_route", ["none", "safe", "manual"])
async def test_budget_preflight_respects_saved_handoff_defaults(responses, select_route):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with NativeProvider(base_url=responses.url, api_key="synthetic-saved-budget") as provider:
        leaf = Agent(name="C", instructions="Read C.", tools=[lookup])
        middle = Agent(name="B", instructions="Route to C.", handoffs=[leaf])
        safe = Agent(name="D", instructions="Answer without tools.")
        root = Agent(name="A", instructions="Route.", handoffs=[middle, safe], provider=provider)
        route = {"none": None, "safe": "transfer_to_d", "manual": "transfer_to_b"}[select_route]
        async with root.session() as session:
            await session.update(tools=[ToolRef(route)] if route else [])
            saved = await session.save_state()
        async with timeout(20):
            if select_route == "manual":
                with pytest.raises(CapabilityError, match="cannot be captured"):
                    await root.run(
                        "Read C.", state=saved, budget=RunBudget(max_requests=5, max_tokens=1000)
                    )
                assert not responses.requests
            else:
                if route:
                    responses.respond(function_call(route, {}, "safe-handoff"))
                responses.respond(assistant_output("No tools"))
                result = await root.run("Read C.", state=saved, budget=RunBudget(max_requests=5))
                assert result.final_output == "No tools"
                assert [tool["name"] for tool in function_declarations(responses.requests[0])] == (
                    [route] if route else []
                )


@pytest.mark.parametrize("budgeted", [False, True])
async def test_restored_session_keeps_empty_handoff_defaults(responses, budgeted):
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-empty-defaults"
    ) as provider:
        target = Agent(name="B", instructions="Answer.")
        root = Agent(name="A", instructions="Route.", handoffs=[target], provider=provider)
        async with root.session() as session:
            await session.update(tools=[])
            saved = await session.save_state()
        responses.respond(assistant_output("No tools"))
        async with root.session(state=saved) as session:
            turn = await session.submit(
                "Answer.", budget=RunBudget(max_requests=5) if budgeted else None
            )
            assert (await turn.get_result()).final_output == "No tools"
        assert not function_declarations(responses.requests[0])


async def test_budgeted_handoff_keeps_live_manual_handler(responses):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    async with NativeProvider(base_url=responses.url, api_key="synthetic-live-budget") as provider:
        target = Agent(name="B", instructions="Read B.", tools=[lookup])
        source = Agent(name="A", instructions="Route to B.", handoffs=[target], provider=provider)
        responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
        responses.respond(function_call("lookup", {"key": "B"}, "lookup-B"))
        responses.respond(assistant_output("Live completed"))
        async with timeout(20):
            result = await source.run(
                "Read B.",
                budget=RunBudget(max_requests=5),
                on_interaction=lambda request: request.reply("live reply"),
            )
        assert result.final_output == "Live completed"
        assert len(responses.requests) == 3
