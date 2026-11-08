"""Code Mode checks only the callable identities exposed by each effective step."""

from __future__ import annotations

import pytest

from agents import Agent, NativeError, WebSearch, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call, code_output


def collision_pair(kind, effects):
    async def first() -> str:
        effects.append("first")
        return "FIRST"

    async def second() -> str:
        effects.append("second")
        return "SECOND"

    if kind == "namespace":
        return (
            function_tool(name="crm__lookup")(first),
            function_tool(name="lookup", namespace="crm")(second),
            "crm__lookup",
        )
    return (
        function_tool(name="foo-bar")(first),
        function_tool(name="foo_bar")(second),
        "foo_bar",
    )


@pytest.mark.parametrize("kind", ["namespace", "bare"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_code_mode_rejects_ambiguous_roots_but_keeps_narrowed_roots_and_steps(
    responses, kind, reverse
):
    effects = []
    first, second, symbol = collision_pair(kind, effects)
    tools = [second, first] if reverse else [first, second]
    agent = Agent(
        instructions="Use the selected callback.",
        tools=tools,
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    agent.validate()
    async with timeout(30), agent.session() as session:
        with pytest.raises(NativeError, match="Code Mode.*collid"):
            await (await session.submit("Both names are ambiguous.")).get_result()
        assert responses.requests == [] and effects == []
        for index, selected in enumerate((first, second)):
            responses.respond(code_call(f"text(await tools.{symbol}({{}}));", f"selected-{index}"))
            responses.respond(assistant_output("selected"))
            turn = await session.submit(
                "Use one callback.",
                tools=[selected.ref] if index == 0 else None,
                before_step=((lambda turn, *, step_index: [second.ref]) if index == 1 else None),
            )
            assert (await turn.get_result()).final_output == "selected"
        assert effects == ["first", "second"]
        assert "FIRST" in code_output(responses.requests[1], "selected-0")
        assert "SECOND" in code_output(responses.requests[3], "selected-1")


@pytest.mark.parametrize("kind", ["namespace", "bare"])
async def test_later_step_collision_fails_before_another_provider_request(responses, kind):
    effects = []
    first, second, symbol = collision_pair(kind, effects)
    responses.respond(code_call(f"text(await tools.{symbol}({{}}));", "first-step"))
    agent = Agent(
        instructions="Use callbacks.",
        tools=[first, second],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(30), agent.session() as session:
        with pytest.raises(NativeError, match="Code Mode.*collid"):
            await (
                await session.submit(
                    "Narrow only the first step.",
                    before_step=lambda turn, *, step_index: (
                        [first.ref] if step_index == 0 else None
                    ),
                )
            ).get_result()
    assert len(responses.requests) == 1 and effects == ["first"]


async def test_narrowed_defaults_survive_update_and_restore_without_banning_the_catalog(responses):
    effects = []
    first, second, symbol = collision_pair("bare", effects)
    agent = Agent(
        instructions="Use callbacks.",
        tools=[first, second],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(30), agent.session() as session:
        await session.update(tools=[first])
        responses.respond(code_call(f"text(await tools.{symbol}({{}}));", "default"))
        responses.respond(assistant_output("saved"))
        assert (
            await (await session.submit("Use the narrowed default.")).get_result()
        ).final_output == "saved"
        state = await session.save_state()
    async with timeout(30), agent.session(state=state) as restored:
        responses.respond(code_call(f"text(await tools.{symbol}({{}}));", "restored"))
        responses.respond(assistant_output("restored"))
        assert (
            await (await restored.submit("Use the saved default.")).get_result()
        ).final_output == "restored"
        await restored.update(tools=[first, second])
        with pytest.raises(NativeError, match="Code Mode.*collid"):
            await (await restored.submit("The defaults now collide.")).get_result()
    assert len(responses.requests) == 4 and effects == ["first", "first"]


@pytest.mark.parametrize("kind", ["namespace", "bare"])
async def test_direct_dispatch_retains_both_exact_identities(responses, kind):
    effects = []
    first, second, _ = collision_pair(kind, effects)
    calls = []
    for index, tool in enumerate((first, second)):
        call = function_call(tool.name, {}, f"direct-{index}")
        if tool.namespace is not None:
            call["item"]["namespace"] = tool.namespace
        calls.append(call)
    responses.respond(*calls)
    responses.respond(assistant_output("direct"))
    agent = Agent(
        instructions="Use both callbacks.", tools=[first, second], provider=provider(responses)
    )
    async with timeout(20), agent.session() as session:
        assert (await (await session.submit("Use both.")).get_result()).final_output == "direct"
    assert sorted(effects) == ["first", "second"]


@pytest.mark.parametrize("excluded", ["collaboration", "hosted-search"])
async def test_noncallable_and_direct_only_capabilities_do_not_create_code_collisions(
    responses, excluded
):
    effects = []

    @function_tool(name="collaboration__lookup" if excluded == "collaboration" else "web-search")
    async def callback() -> str:
        effects.append("callback")
        return "CALLBACK"

    if excluded == "collaboration":

        @function_tool(name="lookup", namespace="collaboration")
        async def direct() -> str:
            pytest.fail("The direct-only callback was invoked through generated code")

        other = direct
        symbol = "collaboration__lookup"
    else:
        other = WebSearch(route="hosted")
        symbol = "web_search"
    responses.respond(code_call(f"text(await tools.{symbol}({{}}));", "allowed"))
    responses.respond(assistant_output("allowed"))
    agent = Agent(
        instructions="Use the callable tool.",
        model="gpt-5.5",
        tools=[callback, other],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(30), agent.session() as session:
        assert (
            await (await session.submit("Use the generated callback.")).get_result()
        ).final_output == "allowed"
    assert effects == ["callback"]
    assert "CALLBACK" in code_output(responses.requests[1], "allowed")
