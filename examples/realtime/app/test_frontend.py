"""Run after `uv run playwright install chromium` with
`uv run pytest -o pythonpath=. examples/realtime/app/test_frontend.py`.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from playwright.async_api import Page, async_playwright, expect

from agents import RunContextWrapper
from agents.realtime.events import RealtimeEventInfo, RealtimeToolEnd
from agents.tool_context import ToolContext
from examples.realtime.app.agent import get_weather, triage_agent, update_seat
from examples.realtime.app.server import RealtimeWebSocketManager

STATIC = Path(__file__).parent / "static"


@pytest_asyncio.fixture(loop_scope="function")
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()

        # Serve only the demo's static files; no server or model connection is opened.
        async def serve(route):
            files = {
                "http://demo.test/": ("index.html", "text/html"),
                "http://demo.test/app.js": ("app.js", "application/javascript"),
            }
            if route.request.url in files:
                name, content_type = files[route.request.url]
                await route.fulfill(path=STATIC / name, content_type=content_type)
            else:
                await route.abort()

        await context.route("**/*", serve)
        await context.add_init_script("""
            window.sentMessages = [];
            window.WebSocket = class {
                static OPEN = 1;
                constructor() { this.readyState = 1; window.demoSocket = this; }
                send(message) { window.sentMessages.push(JSON.parse(message)); }
            };
        """)
        page = await context.new_page()
        await page.goto("http://demo.test/")
        await page.locator("#connectBtn").click()
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


async def receive(page: Page, event: dict[str, Any]) -> None:
    await page.evaluate("data => window.demoSocket.onmessage({data})", json.dumps(event))


async def weather_event(city: str) -> dict[str, Any]:
    arguments = json.dumps({"city": city})
    context = ToolContext(
        context=None, tool_name="get_weather", tool_call_id="weather-1", tool_arguments=arguments
    )
    output = await get_weather.on_invoke_tool(context, arguments)
    return await RealtimeWebSocketManager()._serialize_event(
        RealtimeToolEnd(
            agent=triage_agent,
            tool=get_weather,
            arguments=arguments,
            output=output,
            info=RealtimeEventInfo(context=RunContextWrapper(context=None)),
        )
    )


@pytest.mark.parametrize(
    "city",
    [
        '<img src=x onerror="window.injected=true;window.confirm=()=>true">',
        '<svg onload="window.injected=true;window.confirm=()=>true"></svg>',
        'A & B <district> "north"',
    ],
    ids=["image-handler", "svg-handler", "literal-punctuation"],
)
async def test_weather_output_is_text_and_cannot_replace_approval(page: Page, city: str) -> None:
    assert get_weather.needs_approval is False
    assert update_seat.needs_approval is True
    await receive(page, await weather_event(city))

    panel = page.locator("#toolsContent")
    await expect(panel).to_contain_text(f"get_weather: The weather in {city} is sunny.")
    await expect(panel.locator("img, svg")).to_have_count(0)
    assert await page.evaluate("window.injected === undefined")

    # A completion must not replace the browser's approval dialog or its decision.
    dialogs = []

    async def reject(dialog):
        dialogs.append(dialog.message)
        await dialog.dismiss()

    page.on("dialog", reject)
    await receive(
        page, {"type": "tool_approval_required", "tool": "update_seat", "call_id": "seat-1"}
    )
    assert dialogs == ['Allow tool "update_seat" to run?']
    assert await page.evaluate("window.sentMessages") == [
        {"type": "tool_approval_decision", "call_id": "seat-1", "approve": False}
    ]


async def test_normal_events_and_approval_remain_usable(page: Page) -> None:
    await receive(page, {"type": "tool_start", "tool": "get_weather"})
    await receive(page, await weather_event("San Francisco"))
    await receive(page, {"type": "tool_end", "tool": "empty", "output": ""})
    await receive(page, {"type": "handoff", "from": "Triage Agent", "to": "Seat Booking Agent"})

    async def accept(dialog):
        await dialog.accept()

    page.on("dialog", accept)
    await receive(
        page, {"type": "tool_approval_required", "tool": "update_seat", "call_id": "seat-2"}
    )
    panel = page.locator("#toolsContent")
    for text in [
        "Running get_weather",
        "get_weather: The weather in San Francisco is sunny.",
        "empty: No output",
        "From Triage Agent to Seat Booking Agent",
        "Waiting on update_seat",
        "✅ Approved",
        "update_seat (seat-2)",
    ]:
        await expect(panel).to_contain_text(text)
    await expect(panel.locator(".event-header.tool")).to_have_count(5)
    await expect(panel.locator(".event-header.handoff")).to_have_count(1)
    assert await page.evaluate("window.sentMessages") == [
        {"type": "tool_approval_decision", "call_id": "seat-2", "approve": True}
    ]


async def test_shared_event_descriptions_are_literal(page: Page) -> None:
    name = '<img src=x onerror="window.injected=true">'
    await receive(page, {"type": "tool_start", "tool": name})
    await receive(page, {"type": "handoff", "from": name, "to": name})

    async def reject(dialog):
        await dialog.dismiss()

    page.on("dialog", reject)
    await receive(page, {"type": "tool_approval_required", "tool": name, "call_id": name})
    panel = page.locator("#toolsContent")
    for text in [
        f"Running {name}",
        f"From {name} to {name}",
        f"Waiting on {name}",
        f"{name} ({name})",
    ]:
        await expect(panel).to_contain_text(text)
    await expect(panel.locator("img")).to_have_count(0)
    assert await page.evaluate("window.injected === undefined")
