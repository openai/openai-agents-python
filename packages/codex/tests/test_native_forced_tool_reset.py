"""Explicit forced-choice reset stays within one admitted profile and logical run."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, FunctionTool, NativeProvider, ToolRef, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request


def provider(responses):
    return NativeProvider(base_url=responses.url, api_key="synthetic-reset-key")


def assert_local_control(requests):
    assert all("reset_tool_choice" not in request for request in requests)


@pytest.mark.parametrize("setting", ["omitted", False, None])
async def test_default_choice_keeps_forcing_after_accepted_tool_use(responses, setting):
    effects = []

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    settings = {"tool_choice": "required"}
    if setting != "omitted":
        settings["reset_tool_choice"] = setting
    responses.respond(function_call("echo", {"value": "accepted"}, "persistent"))
    responses.respond(assistant_output("done"))
    async with timeout(15), provider(responses) as native:
        result = await Agent(
            instructions="Use echo.", provider=native, tools=[echo], model_settings=settings
        ).run("go")
    assert result.final_output == "done"
    assert effects == ["accepted"]
    assert [request["tool_choice"] for request in responses.requests] == ["required", "required"]
    assert_local_control(responses.requests)


@pytest.mark.parametrize("named", [False, True])
async def test_opt_in_releases_forcing_and_new_runs_start_with_declared_choice(responses, named):
    effects = []

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    choice = ToolRef("echo") if named else "required"
    for index in range(2):
        responses.respond(function_call("echo", {"value": str(index)}, f"call-{index}"))
        responses.respond(assistant_output("done"))
    async with timeout(15), provider(responses) as native:
        agent = Agent(
            instructions="Use echo.",
            provider=native,
            tools=[echo],
            model_settings={"tool_choice": choice, "reset_tool_choice": True},
        )
        async with agent.session() as session:
            for _ in range(2):
                assert (await (await session.submit("go")).get_result()).final_output == "done"
    assert effects == ["0", "1"]
    for request in responses.requests[::2]:
        if named:
            assert request["tool_choice"]["name"] == "echo"
        else:
            assert request["tool_choice"] == "required"
    assert all(request.get("tool_choice", "auto") == "auto" for request in responses.requests[1::2])
    assert_local_control(responses.requests)


async def test_incoming_handoff_profile_keeps_its_first_named_choice(responses):
    effects = []

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    target = Agent(
        name="B",
        instructions="Use echo.",
        tools=[echo],
        model_settings={"tool_choice": ToolRef("echo"), "reset_tool_choice": True},
    )
    responses.respond(function_call("transfer_to_b", {}, "handoff"))
    responses.respond(function_call("echo", {"value": "B"}, "target"))
    responses.respond(assistant_output("done"))
    async with timeout(15), provider(responses) as native:
        source = Agent(
            name="A",
            instructions="Transfer to B.",
            provider=native,
            handoffs=[target],
            model_settings={"tool_choice": "required", "reset_tool_choice": True},
        )
        assert (await source.run("go")).final_output == "done"
    assert effects == ["B"]
    assert responses.requests[0]["tool_choice"] == "required"
    assert responses.requests[1]["tool_choice"]["name"] == "echo"
    assert responses.requests[2].get("tool_choice", "auto") == "auto"
    assert_local_control(responses.requests)


async def test_manual_restore_releases_choice_without_reexecuting_accepted_sibling(responses):
    effects = []
    entered = asyncio.Event()

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        entered.set()
        return value

    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(
        function_call("echo", {"value": "once"}, "automatic"),
        function_call("lookup", {"key": "pending"}, "manual"),
    )
    responses.respond(assistant_output("done"))
    async with timeout(20), provider(responses) as native:
        agent = Agent(
            instructions="Use both tools.",
            provider=native,
            tools=[echo, lookup],
            model_settings={"tool_choice": "required", "reset_tool_choice": True},
        )
        async with agent.session() as original:
            await original.submit("go")
            request = await next_request(original.interactions(), lookup)
            await entered.wait()
            saved = SessionState.from_json((await original.suspend()).to_json())
        assert effects == ["once"]
        assert len(responses.requests) == 1
        async with agent.session(state=saved) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.call_id == request.call_id
            await current.respond("accepted")
            assert (await restored.wait()).final_output == "done"
    assert effects == ["once"]
    assert len(responses.requests) == 2
    assert responses.requests[0]["tool_choice"] == "required"
    assert responses.requests[1].get("tool_choice", "auto") == "auto"
    assert_local_control(responses.requests)


async def test_none_remains_disabled_when_reset_is_enabled(responses):
    responses.respond(assistant_output("done"))
    async with timeout(15), provider(responses) as native:
        assert (
            await Agent(
                instructions="Answer.",
                provider=native,
                model_settings={"tool_choice": "none", "reset_tool_choice": True},
            ).run("go")
        ).final_output == "done"
    assert responses.requests[0]["tool_choice"] == "none"
    assert_local_control(responses.requests)
