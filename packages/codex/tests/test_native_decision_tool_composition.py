"""Native Computer and managed callbacks inherit exact live decision owners."""

from dataclasses import replace

import pytest

from agents import (
    Agent,
    Allow,
    ComputerProvider,
    ToolContext,
    computer_tool,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,  # noqa: PLC0414
)
from tests.test_decision_inherited_callbacks import Provider
from tests.test_native_computer import computer_call
from tests.test_native_computer_adapter import Driver
from tests.test_native_manual_tools import next_request, provider


async def check_exact_owner(context: ToolContext[None], question: str) -> None:
    # A copy retains observed metadata, without inheriting this callback's authority.
    with pytest.raises(RuntimeError, match="exact live callback"):
        await replace(context).decide("Foreign owner?", input="Synthetic policy")
    assert await context.decide(question, input="Synthetic policy") is True


async def test_managed_before_and_approval_predicate_inherit_live_decision_owner(
    responses,
):
    decisions, captured, effects = Provider(), [], []

    async def before(context: ToolContext[None]):
        if captured and captured[0] is not context:
            with pytest.raises(RuntimeError, match="exact live callback"):
                await captured[0].decide("Earlier owner?", input="Synthetic policy")
        captured.append(context)
        await check_exact_owner(context, "Before function?")
        return Allow()

    async def needs_approval(context: ToolContext[None]):
        captured.append(context)
        await check_exact_owner(context, "Request approval?")
        return True

    @function_tool(
        before=before,
        needs_approval=needs_approval,
        run_before_approval=True,
    )
    async def risky(context: ToolContext[None], value: str) -> str:
        effects.append((context.call_id, value))
        return value

    responses.respond(function_call("risky", {"value": "once"}, "managed-decision"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        provider(responses) as model,
        Agent(
            instructions="Use risky once.",
            provider=model,
            decisions=decisions,
            tools=[risky],
        ).session() as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        assert effects == []
        assert len(decisions.requests) == 2
        await request.approve()
        assert (await turn.get_result()).final_output == "done"
        assert all(context.submission is turn for context in captured)
        assert all(context.call.call_id == "managed-decision" for context in captured)
        assert all(context.call.session_id == session.id for context in captured)
        for context in captured:
            with pytest.raises(RuntimeError, match="exact live callback"):
                await context.decide("Expired?", input="Synthetic policy")
    assert [request.question for request in decisions.requests] == [
        "Before function?",
        "Request approval?",
        "Before function?",
    ]
    assert effects == [("managed-decision", "once")]
    assert len(responses.requests) == 2


async def test_computer_hooks_and_provider_creation_inherit_live_decision_owner(
    responses,
):
    decisions, driver, captured, disposed = Provider(), Driver(), [], []

    async def approve(context, actions):
        assert actions == [{"type": "click", "x": 1, "y": 2, "button": "left"}]
        captured.append(context)
        await check_exact_owner(context, "Approve computer?")
        return True

    async def safety(context, checks):
        assert checks == [{"id": "safety-decision", "message": "confirm"}]
        captured.append(context)
        await check_exact_owner(context, "Acknowledge checks?")
        return True

    async def create(context):
        captured.append(context)
        await check_exact_owner(context, "Create computer?")
        return driver

    computer = computer_tool(
        ComputerProvider(create, disposed.append),
        approve=approve,
        on_safety_check=safety,
    )
    responses.respond(
        computer_call(
            action={"type": "click", "x": 1, "y": 2, "button": "left"},
            pending_safety_checks=[{"id": "safety-decision", "message": "confirm"}],
        )
    )
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        provider(responses) as model,
        Agent(
            instructions="Use computer once.",
            provider=model,
            decisions=decisions,
            tools=[computer],
        ).session() as session,
    ):
        turn = await session.submit("click")
        assert (await turn.get_result()).final_output == "done"
        assert len(captured) == 3
        assert all(context is captured[0] for context in captured)
        assert captured[0].submission is turn
        assert captured[0].call.call_id == "native-computer"
        assert captured[0].call.session_id == session.id
        with pytest.raises(RuntimeError, match="exact live callback"):
            await captured[0].decide("Expired?", input="Synthetic policy")
    assert [request.question for request in decisions.requests] == [
        "Approve computer?",
        "Acknowledge checks?",
        "Create computer?",
    ]
    assert [call[0] for call in driver.calls] == ["click", "screenshot"]
    assert disposed == [driver]
    assert len(responses.requests) == 2
