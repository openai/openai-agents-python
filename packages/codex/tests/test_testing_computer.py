"""Public scripted computer items execute and replay through the native loop."""

import pytest

from agents import Agent, ComputerProvider, ComputerSafetyError, computer_tool
from agents.testing import ScriptedResponses, computer_call, text
from tests.test_native_computer_adapter import Driver


async def test_scripted_computer_actions_screenshot_and_native_history_replay():
    actions = [{"type": "click", "x": 1, "y": 2, "button": "left"}, {"type": "screenshot"}]
    checks = [{"id": "synthetic-check", "code": "domain", "message": "confirm"}]
    item = computer_call(
        actions, call_id="computer-1", item_id="cu_script", pending_safety_checks=checks
    )
    actions[0]["x"] = 99
    checks[0]["message"] = "later mutation"
    driver, disposed = Driver(), []
    application_data = {"private_note": "synthetic-private-computer-tag"}

    def extract(_, output):
        assert output["type"] == "computer_call_output"
        assert output["call_id"] == "computer-1"
        assert output["output"]["image_url"] == "data:image/png;base64,aA=="
        output["output"]["image_url"] = "data:image/png;base64,detached"
        return application_data

    async with ScriptedResponses([item], [text("clicked")], [text("remembered")]) as peer:
        agent = Agent(
            instructions="Use the computer.",
            model="gpt-5",
            provider=peer.provider,
            tools=[
                computer_tool(
                    ComputerProvider(lambda _: driver, disposed.append),
                    on_safety_check=lambda *_: True,
                    extract_application_data=extract,
                )
            ],
        )
        async with agent.session() as session:
            completed = await (await session.submit("Click.")).get_result()
            assert completed.final_output == "clicked"
            (record,) = completed.application_tool_outputs
            assert record.call_id == "computer-1" and record.name == "computer"
            assert record.application_data == application_data
            assert (await session.save_state()).application_tool_outputs == [record]
            history = [item async for item in session.items()]
            normalized = next(item for item in history if item.get("type") == "function_call")
            assert normalized["name"] == "computer"
            assert normalized["call_id"] == "computer-1"
            assert normalized["arguments"]["type"] == "computer_call"
            assert normalized["arguments"]["actions"][0]["x"] == 1
            assert (
                await (await session.submit("Remember.")).get_result()
            ).final_output == "remembered"
        for request in peer.requests[1:]:
            assert "synthetic-private-computer-tag" not in repr(request)
            output = next(
                item for item in request["input"] if item.get("type") == "computer_call_output"
            )
            assert output["call_id"] == "computer-1"
            assert output["output"] == {
                "type": "computer_screenshot",
                "image_url": "data:image/png;base64,aA==",
            }
            assert output["acknowledged_safety_checks"][0]["message"] == "confirm"
        assert driver.calls == [("click", (1, 2, "left"), {}), ("screenshot",)]
        assert disposed == [driver]


async def test_scripted_computer_safety_still_denies_before_device_acquisition():
    created = []
    async with ScriptedResponses(
        [
            computer_call(
                [{"type": "screenshot"}],
                call_id="denied",
                pending_safety_checks=[{"id": "deny", "message": "confirm"}],
            )
        ]
    ) as peer:
        agent = Agent(
            instructions="Use the computer.",
            model="gpt-5",
            provider=peer.provider,
            tools=[computer_tool(ComputerProvider(lambda _: created.append(Driver())))],
        )
        with pytest.raises(ComputerSafetyError):
            await agent.run("Look.")
        assert created == []
