import asyncio
import json
from typing import Any, cast

import pytest

from agents import Agent, RunConfig, Runner
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.plugins.browser.src.agents_browser import browser
from examples.plugins.browser.src.agents_browser.example import browser_for_run


@pytest.mark.asyncio
async def test_browser_runs_only_registered_actions_and_bounds_the_answer() -> None:
    seen = []

    async def hours() -> str:
        seen.append("hours")
        return "9 to 5. " * 250 + "overflow"

    async def failed() -> str:
        raise RuntimeError("private page contents")

    async def metadata() -> Any:
        return {"internal": "private metadata" * 200}

    actions = {"hours": hours, "failed": failed, "metadata": metadata}
    plugin = browser(actions)
    actions["file:///private"] = hours
    model = ScriptedModel(
        [
            [function_call("browser_read", {"action": "file:///private"}, call_id="deny")],
            [function_call("browser_read", {"action": "hours"}, call_id="read")],
            [function_call("browser_read", {"action": "failed"}, call_id="error")],
            [function_call("browser_read", {"action": "metadata"}, call_id="metadata")],
            [assistant_message("Done.")],
        ]
    )
    result = await Runner.run(
        Agent("Desk", model=model, plugins=[plugin]),
        "Check the hours.",
        run_config=RunConfig(tracing_disabled=True),
    )
    denied, answer, error, invalid = (
        item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
    )
    assert denied == "Tool call refused."
    assert json.loads(answer) == {"action": "hours", "text": "9 to 5. " * 250 + "…"}
    assert error == invalid == "Browser read failed."
    assert "private page contents" not in str(model.calls)
    assert "private metadata" not in str(model.calls)
    assert seen == ["hours"]


@pytest.mark.asyncio
async def test_browser_scope_waits_for_cancelled_reads_before_client_teardown() -> None:
    started, returned, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed: list[str] = []

    async def hours() -> str:
        # Hold the context open past Runner cancellation without relying on browser timing.
        started.set()
        try:
            await release.wait()
            return "9 to 5."
        except asyncio.CancelledError:
            await release.wait()
            closed.append("context")
            raise

    model = ScriptedModel([[function_call("browser_read", {"action": "hours"}, call_id="read")]])

    async def run() -> None:
        try:
            async with browser_for_run({"hours": hours}) as plugin:
                agent = Agent("Desk", model=model, plugins=[plugin])
                try:
                    await Runner.run(
                        agent, "Check the hours.", run_config=RunConfig(tracing_disabled=True)
                    )
                finally:
                    returned.set()
        finally:
            closed.append("browser")

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(returned.wait(), 5)
        assert not closed
    finally:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert closed == ["context", "browser"]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_cancelling_context_creation_closes_only_that_context() -> None:
    pytest.importorskip("playwright.async_api")
    from examples.plugins.browser.src.agents_browser.panels import read_panel

    # Pause after the browser has created the context but before Playwright returns it.
    created, release = asyncio.Event(), asyncio.Event()

    class Context:
        async def close(self) -> None:
            read.cancel()
            await asyncio.sleep(0)
            instance.contexts.remove(self)

    sibling = Context()

    class Browser:
        def __init__(self) -> None:
            self.contexts = [sibling]

        async def new_context(self, **options: bool) -> Context:
            context = Context()
            self.contexts.append(context)
            created.set()
            await release.wait()
            return context

    instance = Browser()
    read = asyncio.create_task(read_panel(cast(Any, instance), html="", label="opening"))
    try:
        await asyncio.wait_for(created.wait(), 5)
    finally:
        read.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await read
    assert instance.contexts == [sibling]
