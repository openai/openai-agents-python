"""Read a local FAQ in Chromium with a scripted model."""

import asyncio
import json
from functools import partial

from playwright.async_api import async_playwright

from agents import Agent, RunConfig, Runner
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel, assistant_message, function_call

from . import browser
from .panels import read_panel

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


async def main(channel: str | None = None) -> None:
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
            agent = Agent("Desk", model=model, plugins=[browser({"hours": hours})])
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
