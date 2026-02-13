# How to run this example:
# uv run python -m playwright install chromium
# uv run -m examples.tools.computer_use

import asyncio
import base64
import sys
from typing import Any, Literal, Union

from playwright.async_api import Browser, Page, Playwright, async_playwright

from agents import (
    Agent,
    AsyncComputer,
    Button,
    ComputerProvider,
    ComputerTool,
    Environment,
    ModelSettings,
    RunContextWrapper,
    Runner,
    trace,
)

# Uncomment to see very verbose logs
# import logging
# logging.getLogger("openai.agents").setLevel(logging.DEBUG)
# logging.getLogger("openai.agents").addHandler(logging.StreamHandler())


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
        self._playwright: Union[Playwright, None] = None
        self._browser: Union[Browser, None] = None
        self._page: Union[Page, None] = None

    async def _get_browser_and_page(self) -> tuple[Browser, Page]:
        width, height = self.dimensions
        launch_args = [f"--window-size={width},{height}"]
        browser = await self.playwright.chromium.launch(headless=False, args=launch_args)
        page = await browser.new_page()
        await page.set_viewport_size({"width": width, "height": height})
        await page.goto("https://www.bing.com")
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
    def environment(self) -> Environment:
        return "browser"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (1024, 768)

    async def screenshot(self) -> str:
        """Capture only the viewport (not full_page)."""
        png_bytes = await self.page.screenshot(full_page=False)
        return base64.b64encode(png_bytes).decode("utf-8")

    async def click(self, x: int, y: int, button: Button = "left") -> None:
        playwright_button: Literal["left", "middle", "right"] = "left"

        # Playwright only supports left, middle, right buttons
        if button in ("left", "right", "middle"):
            playwright_button = button  # type: ignore

        await self.page.mouse.click(x, y, button=playwright_button)

    async def double_click(self, x: int, y: int) -> None:
        await self.page.mouse.dblclick(x, y)

    async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        await self.page.mouse.move(x, y)
        await self.page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    async def type(self, text: str) -> None:
        await self.page.keyboard.type(text)

    async def wait(self) -> None:
        await asyncio.sleep(1)

    async def move(self, x: int, y: int) -> None:
        await self.page.mouse.move(x, y)

    async def keypress(self, keys: list[str]) -> None:
        mapped_keys = [CUA_KEY_TO_PLAYWRIGHT_KEY.get(key.lower(), key) for key in keys]
        for key in mapped_keys:
            await self.page.keyboard.down(key)
        for key in reversed(mapped_keys):
            await self.page.keyboard.up(key)

    async def drag(self, path: list[tuple[int, int]]) -> None:
        if not path:
            return
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
            instructions="You are a helpful agent. Find the current weather in Tokyo.",
            tools=[ComputerTool(computer=computer_config)],
            # Use the computer using model, and set truncation to auto because it is required.
            model="computer-use-preview",
            model_settings=ModelSettings(truncation="auto"),
        )
        result = await Runner.run(agent, "What is the weather in Tokyo right now?")
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
