"""Real native Responses dispatch, continuation and computer resource ownership."""

import asyncio

import pytest

from agents import (
    Agent,
    ComputerProvider,
    ComputerSafetyError,
    NativeProvider,
    ToolRef,
    computer_tool,
)
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_computer_adapter import Driver


def computer_call(**fields):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "computer_call",
            "id": "cu_native",
            "call_id": "native-computer",
            "status": "completed",
            **fields,
        },
    }


@pytest.mark.parametrize("tool_type", ["computer", "computer_use_preview"])
@pytest.mark.parametrize("choice", ["auto", "required", ToolRef("computer")])
async def test_computer_native_wire_and_acknowledged_continuation(responses, tool_type, choice):
    driver, created, disposed = Driver(), [], []
    checks = [{"id": "safety-1", "code": "domain", "message": "confirm", "future": {"keep": True}}]

    def create(_):
        created.append(driver)
        return driver

    tool = computer_tool(
        ComputerProvider(create, disposed.append),
        tool_type=tool_type,
        dimensions=(800, 600),
        environment="browser",
        on_safety_check=lambda *_: True,
    )
    action = {"type": "click", "x": 1, "y": 2, "button": "left"}
    payload = (
        {"actions": [action, {"type": "screenshot"}]}
        if tool_type == "computer"
        else {"action": action}
    )
    original = computer_call(
        **payload, pending_safety_checks=checks, future_provider_field="preserved"
    )
    responses.respond(original)
    responses.respond(assistant_output("done"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    agent = Agent(
        instructions="Use the computer.",
        provider=provider,
        tools=[tool],
        model_settings={"tool_choice": choice},
    )
    try:
        assert (await agent.run("click")).final_output == "done"
    finally:
        await provider.aclose()
    native_tools = [
        item
        for item in responses.requests[0]["tools"]
        if item.get("type") in ("computer", "computer_use_preview")
    ]
    assert native_tools == (
        [{"type": "computer"}]
        if tool_type == "computer"
        else [
            {
                "type": "computer_use_preview",
                "display_width": 800,
                "display_height": 600,
                "environment": "browser",
            }
        ]
    )
    assert not any(
        item.get("type") == "function" and item.get("name") == "computer"
        for item in responses.requests[0]["tools"]
    )
    assert responses.requests[0]["tool_choice"] == (
        "required" if isinstance(choice, ToolRef) else choice
    )
    retained = responses.requests[1]["input"]
    assert (
        next(item for item in retained if item.get("type") == "computer_call")[
            "future_provider_field"
        ]
        == "preserved"
    )
    output = next(item for item in retained if item.get("type") == "computer_call_output")
    assert output["call_id"] == "native-computer"
    assert output["output"] == {
        "type": "computer_screenshot",
        "image_url": "data:image/png;base64,aA==",
    }
    assert output["acknowledged_safety_checks"] == checks
    assert [call[0] for call in driver.calls] == ["click", "screenshot"]
    assert created == disposed == [driver]


async def test_computer_pending_safety_rejects_before_native_driver_acquisition(responses):
    effects = []
    tool = computer_tool(ComputerProvider(lambda _: effects.append("create")))
    responses.respond(
        computer_call(
            action={"type": "wait"}, pending_safety_checks=[{"id": "s1", "message": "confirm"}]
        )
    )
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    try:
        with pytest.raises(ComputerSafetyError):
            await Agent(instructions="Use computer.", provider=provider, tools=[tool]).run("wait")
    finally:
        await provider.aclose()
    assert effects == []
    assert len(responses.requests) == 1


@pytest.mark.parametrize("choice", [ToolRef("unavailable"), ToolRef("computer", "foreign")])
async def test_named_computer_choice_requires_the_current_native_declaration(responses, choice):
    effects = []
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    try:
        with pytest.raises((RuntimeError, ValueError)):
            await Agent(
                instructions="Use computer.",
                provider=provider,
                tools=[computer_tool(ComputerProvider(lambda _: effects.append("create")))],
                model_settings={"tool_choice": choice},
            ).run("wait")
    finally:
        await provider.aclose()
    assert effects == []
    assert responses.requests == []


async def test_native_cancellation_joins_acquired_driver_before_exactly_once_disposal(responses):
    entered, release = asyncio.Event(), asyncio.Event()
    disposed = []

    class AsyncDriver(Driver):
        async def wait(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()

    driver = AsyncDriver()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    responses.respond(computer_call(action={"type": "wait"}))
    agent = Agent(
        instructions="Use computer.",
        provider=provider,
        tools=[computer_tool(ComputerProvider(lambda _: driver, disposed.append))],
    )
    async with agent.session() as session:
        await session.submit("wait")
        await asyncio.wait_for(entered.wait(), 10)
        close = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        assert disposed == []
        release.set()
        await asyncio.wait_for(close, 10)
        await session.aclose()
    await provider.aclose()
    assert disposed == [driver]
    assert driver.calls == []
    assert len(responses.requests) == 1
