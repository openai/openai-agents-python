# How to run this example:
# uv run python -m playwright install chromium
# uv run -m examples.tools.computer_use

import asyncio
import base64
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from playwright.async_api import Browser, Page, Playwright, async_playwright

from agents import (
    Agent,
    AsyncComputer,
    Button,
    ComputerProvider,
    ComputerTool,
    ModelSettings,
    RunContextWrapper,
    Runner,
    trace,
)

# Uncomment to see very verbose logs
# import logging
# logging.getLogger("openai.agents").setLevel(logging.DEBUG)
# logging.getLogger("openai.agents").addHandler(logging.StreamHandler())

HEADLESS = os.environ.get("COMPUTER_USE_HEADLESS") != "0"
START_URL = os.environ.get("COMPUTER_USE_START_URL")
BROWSER_CHANNEL = os.environ.get("COMPUTER_USE_BROWSER_CHANNEL", "chromium")
DEMO_PAGE_HTML = """<!doctype html>
<html>
  <head>
    <title>Tokyo Weather Demo</title>
    <style>
      body {
        font-family: system-ui, sans-serif;
        margin: 40px;
      }
      section {
        max-width: 520px;
      }
      button {
        font: inherit;
        padding: 8px 12px;
      }
    </style>
    <script>
      function refreshForecast() {
        document.querySelector('[data-testid="status"]').textContent =
          'Forecast refreshed at demo time.';
        document.querySelector('[data-testid="current"]').textContent =
          'Current conditions: partly cloudy, 22C.';
        document.querySelector('[data-testid="details"]').textContent =
          'Wind: 37 km/h. Visibility: 10 km. Precipitation: 0.1 mm.';
        document.querySelector('[data-testid="outlook"]').hidden = false;
      }
    </script>
  </head>
  <body>
    <section>
      <h1>Tokyo Weather Demo</h1>
      <p data-testid="status">Forecast pending.</p>
      <button type="button" onclick="refreshForecast()">Refresh forecast</button>
      <p data-testid="current">Current conditions: not loaded.</p>
      <p data-testid="details">Details: not loaded.</p>
      <div data-testid="outlook" hidden>
        <h2>Today</h2>
        <ul>
          <li>Morning: partly cloudy, 19C.</li>
          <li>Noon: sunny, 20C.</li>
          <li>Evening: partly cloudy, 20C.</li>
          <li>Night: clear, 19C.</li>
        </ul>
      </div>
    </section>
  </body>
</html>"""
AGENT_INSTRUCTIONS = "You are a helpful agent. Use the browser computer tool to inspect web pages."
WEATHER_PROMPT = (
    "Use the browser computer tool to click the Refresh forecast button, then summarize "
    "the Tokyo weather shown on the page."
)


CUA_KEY_TO_PLAYWRIGHT_KEY = {
    "/": "Divide",
    "\\": "Backslash",
    "alt": "Alt",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "arrowup": "ArrowUp",
    "backspace": "Backspace",
    "capslock": "CapsLock",
    "cmd": "Meta",
    "ctrl": "Control",
    "delete": "Delete",
    "end": "End",
    "enter": "Enter",
    "esc": "Escape",
    "home": "Home",
    "insert": "Insert",
    "option": "Alt",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "shift": "Shift",
    "space": " ",
    "super": "Meta",
    "tab": "Tab",
    "win": "Meta",
}


class LocalPlaywrightComputer(AsyncComputer):
    """A computer, implemented using a local Playwright browser."""

    def __init__(self):
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    async def _get_browser_and_page(self) -> tuple[Browser, Page]:
        width, height = self.dimensions
        launch_args = [f"--window-size={width},{height}"]
        browser = await self.playwright.chromium.launch(
            channel=BROWSER_CHANNEL,
            headless=HEADLESS,
            args=launch_args,
        )
        page = await browser.new_page()
        await page.set_viewport_size({"width": width, "height": height})
        if START_URL:
            await page.goto(START_URL, wait_until="domcontentloaded")
        else:
            await page.set_content(DEMO_PAGE_HTML, wait_until="domcontentloaded")
        return browser, page

    async def __aenter__(self):
        # Start Playwright and call the subclass hook for getting browser/page
        self._playwright = await async_playwright().start()
        self._browser, self._page = await self._get_browser_and_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        return None

    async def open(self) -> "LocalPlaywrightComputer":
        """Open resources without using a context manager."""
        await self.__aenter__()
        return self

    async def close(self) -> None:
        """Close resources without using a context manager."""
        await self.__aexit__(None, None, None)

    @property
    def playwright(self) -> Playwright:
        assert self._playwright is not None
        return self._playwright

    @property
    def browser(self) -> Browser:
        assert self._browser is not None
        return self._browser

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page

    @property
    def dimensions(self) -> tuple[int, int]:
        return (1024, 768)

    async def screenshot(self) -> str:
        """Capture only the viewport (not full_page)."""
        png_bytes = await self.page.screenshot(full_page=False)
        return base64.b64encode(png_bytes).decode("utf-8")

    def _normalize_keys(self, keys: list[str] | None) -> list[str]:
        if not keys:
            return []
        return [CUA_KEY_TO_PLAYWRIGHT_KEY.get(key.lower(), key) for key in keys]

    @asynccontextmanager
    async def _hold_keys(self, keys: list[str] | None) -> AsyncIterator[None]:
        mapped_keys = self._normalize_keys(keys)
        try:
            for key in mapped_keys:
                await self.page.keyboard.down(key)
            yield
        finally:
            for key in reversed(mapped_keys):
                await self.page.keyboard.up(key)

    async def click(
        self, x: int, y: int, button: Button = "left", *, keys: list[str] | None = None
    ) -> None:
        playwright_button: Literal["left", "middle", "right"] = "left"

        # Playwright only supports left, middle, right buttons
        if button in ("left", "right", "middle"):
            playwright_button = button  # type: ignore

        async with self._hold_keys(keys):
            await self.page.mouse.click(x, y, button=playwright_button)

    async def double_click(self, x: int, y: int, *, keys: list[str] | None = None) -> None:
        async with self._hold_keys(keys):
            await self.page.mouse.dblclick(x, y)

    async def scroll(
        self,
        x: int,
        y: int,
        scroll_x: int,
        scroll_y: int,
        *,
        keys: list[str] | None = None,
    ) -> None:
        async with self._hold_keys(keys):
            await self.page.mouse.move(x, y)
            await self.page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    async def type(self, text: str) -> None:
        await self.page.keyboard.type(text)

    async def wait(self) -> None:
        await asyncio.sleep(1)

    async def move(self, x: int, y: int, *, keys: list[str] | None = None) -> None:
        async with self._hold_keys(keys):
            await self.page.mouse.move(x, y)

    async def keypress(self, keys: list[str]) -> None:
        mapped_keys = self._normalize_keys(keys)
        for key in mapped_keys:
            await self.page.keyboard.down(key)
        for key in reversed(mapped_keys):
            await self.page.keyboard.up(key)

    async def drag(self, path: list[tuple[int, int]], *, keys: list[str] | None = None) -> None:
        if not path:
            return
        async with self._hold_keys(keys):
            await self.page.mouse.move(path[0][0], path[0][1])
            await self.page.mouse.down()
            for px, py in path[1:]:
                await self.page.mouse.move(px, py)
            await self.page.mouse.up()


async def run_agent(
    computer_config: ComputerProvider[LocalPlaywrightComputer] | AsyncComputer,
) -> None:
    with trace("Computer use example"):
        agent = Agent(
            name="Browser user",
            instructions=AGENT_INSTRUCTIONS,
            tools=[ComputerTool(computer=computer_config)],
            # GPT-5.4 uses the built-in Responses API computer tool.
            model="gpt-5.5",
            model_settings=ModelSettings(tool_choice="required"),
        )
        result = await Runner.run(agent, WEATHER_PROMPT)
        print(result.final_output)


async def singleton_computer() -> None:
    # Use a shared computer when you do not expect to run multiple agents concurrently.
    async with LocalPlaywrightComputer() as computer:
        await run_agent(computer)


async def computer_per_request() -> None:
    # Initialize a new computer per request to avoid sharing state between runs.
    async def create_computer(*, run_context: RunContextWrapper[Any]) -> LocalPlaywrightComputer:
        print(f"Creating computer for run context: {run_context}")
        return await LocalPlaywrightComputer().open()

    async def dispose_computer(
        *,
        run_context: RunContextWrapper[Any],
        computer: LocalPlaywrightComputer,
    ) -> None:
        print(f"Disposing computer for run context: {run_context}")
        await computer.close()

    await run_agent(
        ComputerProvider[LocalPlaywrightComputer](
            create=create_computer,
            dispose=dispose_computer,
        )
    )


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mode == "singleton":
        asyncio.run(singleton_computer())
    else:
        asyncio.run(computer_per_request())
