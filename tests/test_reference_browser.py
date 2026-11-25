import asyncio
import json

import pytest

from agents import Agent, RunConfig, Runner
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.plugins.browser.src.agents_browser import browser


@pytest.mark.asyncio
async def test_browser_runs_only_registered_actions_and_bounds_the_answer() -> None:
    seen = []

    async def hours() -> str:
        seen.append("hours")
        return "9 to 5. " * 250 + "overflow"

    async def failed() -> str:
        raise RuntimeError("private page contents")

    actions = {"hours": hours, "failed": failed}
    plugin = browser(actions)
    actions["file:///private"] = hours
    model = ScriptedModel(
        [
            [function_call("browser_read", {"action": "file:///private"}, call_id="deny")],
            [function_call("browser_read", {"action": "hours"}, call_id="read")],
            [function_call("browser_read", {"action": "failed"}, call_id="error")],
            [assistant_message("Done.")],
        ]
    )
    result = await Runner.run(
        Agent("Desk", model=model, plugins=[plugin]),
        "Check the hours.",
        run_config=RunConfig(tracing_disabled=True),
    )
    denied, answer, error = (
        item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
    )
    assert denied == "Tool call refused."
    assert json.loads(answer) == {"action": "hours", "text": "9 to 5. " * 250 + "…"}
    assert error == "Browser read failed."
    assert "private page contents" not in str(model.calls)
    assert seen == ["hours"]


@pytest.mark.asyncio
async def test_cancelling_a_browser_read_reaches_service_cleanup() -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    async def hours() -> str:
        # Pause the app service during an active read without depending on browser timing.
        started.set()
        try:
            return await asyncio.Future[str]()
        finally:
            stopped.set()

    model = ScriptedModel([[function_call("browser_read", {"action": "hours"}, call_id="read")]])
    agent = Agent("Desk", model=model, plugins=[browser({"hours": hours})])
    task = asyncio.create_task(
        Runner.run(agent, "Check the hours.", run_config=RunConfig(tracing_disabled=True))
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.wait_for(stopped.wait(), 5)
    assert len(model.calls) == 1
