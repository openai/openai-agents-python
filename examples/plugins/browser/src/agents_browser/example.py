"""Read a local FAQ in Chromium with a scripted model."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from agents import Agent, RunConfig, Runner
from agents.items import ToolCallOutputItem
from agents.memory import await_session_mutation
from agents.plugins import Plugin
from agents.testing import ScriptedModel, assistant_message, function_call

from . import browser

FAQ = """
<details data-testid="opening">
  <summary>Opening hours</summary>
  <p>Weekdays from 9 to 5.</p>
</details>
<details data-testid="parking">
  <summary>Parking</summary>
  <p>North entrance.</p>
</details>
"""


@asynccontextmanager
async def browser_for_run(
    actions: Mapping[str, Callable[[], Awaitable[str]]],
) -> AsyncIterator[Plugin[Any]]:
    """Keep the app's browser alive until this scope's reads finish cleaning up."""
    pending: set[asyncio.Future[None]] = set()
    closed = False

    def track(action: Callable[[], Awaitable[str]]) -> Callable[[], Awaitable[str]]:
        async def read() -> str:
            if closed:
                raise RuntimeError("The browser scope has closed.")
            done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            pending.add(done)
            try:
                return await action()
            finally:
                pending.remove(done)
                done.set_result(None)

        return read

    try:
        yield browser({name: track(action) for name, action in actions.items()})
    finally:
        # Runner cancellation can return while an action is still closing its context.
        closed = True
        if pending:
            await await_session_mutation(asyncio.gather(*pending))


async def main(channel: str | None = None) -> None:
    from playwright.async_api import async_playwright

    from .panels import read_panel

    model = ScriptedModel(
        [
            [function_call("browser_read", {"action": "hours"}, call_id="hours")],
            [assistant_message("Done.")],
        ]
    )
    async with async_playwright() as playwright:
        async with await playwright.chromium.launch(
            channel=channel, chromium_sandbox=True
        ) as instance:
            hours = partial(read_panel, instance, html=FAQ, label="opening")
            async with browser_for_run({"hours": hours}) as plugin:
                agent = Agent("Desk", model=model, plugins=[plugin])
                result = await Runner.run(
                    agent, "Check the hours.", run_config=RunConfig(tracing_disabled=True)
                )
            (output,) = (
                item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
            )
            assert json.loads(output) == {"action": "hours", "text": "Weekdays from 9 to 5."}
            assert not instance.contexts
            print(output)


if __name__ == "__main__":
    asyncio.run(main())
