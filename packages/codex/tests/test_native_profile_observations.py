"""Committed native Agent changes remain ordered and independent of settled state."""

import asyncio

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.observation import AgentProfileChanged, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


async def test_native_profile_changes_are_immediate_ordered_and_captured(responses):
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await release.wait()
        return "released"

    c = Agent(name="C", instructions="C", tools=[hold])
    b = Agent(name="B", instructions="B", handoffs=[lambda: b, c])
    a = Agent(
        name="A",
        instructions="A",
        handoffs=[b],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-profile-observation"),
    )
    for name, call_id in [
        ("transfer_to_b", "ab"),
        ("transfer_to_b", "bb"),
        ("transfer_to_c", "bc"),
        ("hold", "hold"),
    ]:
        responses.respond(function_call(name, {}, call_id))
    responses.respond(assistant_output("done"))
    async with timeout(20), a.session() as session:
        reader = session.events()
        turn = await session.submit("Switch twice and wait")
        try:
            await entered.wait()
            # get_active_agent returns the public original, not the captured declaration.
            active = await session.get_active_agent()
            assert active is c
            with pytest.raises(AttributeError):
                active.name = "C changed publicly"
            with pytest.raises(AttributeError):
                b.name = "B changed publicly"
            notices = []
            while len(notices) < 2:
                event = await anext(reader)
                assert not isinstance(event, TurnFinished)
                if isinstance(event, AgentProfileChanged):
                    notices.append(event)
            assert [(e.agent_name, e.profile_id, e.profile_epoch) for e in notices] == [
                ("B", "agent_1", 1),
                ("C", "agent_2", 3),
            ]
            assert all(
                e.session_id == session.id
                and e.turn_id == turn.turn_id
                and e.submission_id == turn.submission_id
                for e in notices
            )
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "done"
        await session.aclose()
        assert not any(isinstance(e, AgentProfileChanged) for e in [e async for e in reader])


async def test_idle_profile_changes_preserve_unnamed_root_and_ignore_failed_activation(responses):
    target = Agent(name="B", instructions="B")
    root = Agent(
        instructions="root",
        handoffs=[target],
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-idle-profile-observation"
        ),
    )
    async with timeout(20), root.session() as session:
        reader = session.events()
        await session.update(agent=target)
        await session.update(agent=target)
        with pytest.raises(ValueError):
            await session.update(agent=Agent(name="outside", instructions="outside"))
        await session.update(agent=root)
        assert await session.get_active_agent() is root
        await session.aclose()
        notices = [e async for e in reader if isinstance(e, AgentProfileChanged)]
        assert [(e.agent_name, e.profile_id, e.profile_epoch) for e in notices] == [
            ("B", "agent_1", 1),
            (None, "agent_0", 3),
        ]
        assert all(
            e.session_id == session.id and e.turn_id is None and e.submission_id is None
            for e in notices
        )


async def test_aborted_tool_batch_does_not_publish_uncommitted_agent_change(responses):
    entered = asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await asyncio.Future()
        return "unreachable"

    target = Agent(name="B", instructions="B")
    source = Agent(
        name="A",
        instructions="A",
        tools=[hold],
        handoffs=[target],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-aborted-profile"),
    )
    responses.respond(
        function_call("transfer_to_b", {}, "handoff"), function_call("hold", {}, "hold")
    )
    async with timeout(20), source.session() as session:
        reader = session.events()
        turn = await session.submit("Prepare a handoff and wait")
        await entered.wait()
        await turn.cancel()
        assert await session.get_active_agent() is source
        await session.aclose()
        assert not any(isinstance(e, AgentProfileChanged) for e in [e async for e in reader])
