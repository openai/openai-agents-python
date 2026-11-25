"""Playwright adapter for trusted offline FAQ pages."""

from contextlib import AsyncExitStack

from playwright.async_api import Browser, BrowserContext

from agents.memory import await_session_mutation


async def read_panel(instance: Browser, *, html: str, label: str) -> str:
    """Open one collapsed details panel by its test ID and read its visible paragraph."""
    async with AsyncExitStack() as stack:

        async def open_context() -> BrowserContext:
            context = await instance.new_context(
                offline=True, java_script_enabled=False, accept_downloads=False
            )
            stack.push_async_callback(lambda: await_session_mutation(context.close()))
            return context

        # Register cleanup before cancellation can discard the new context.
        context = await await_session_mutation(open_context())
        context.set_default_timeout(5000)
        page = await context.new_page()
        await page.set_content(html)
        panel = page.get_by_test_id(label)
        await panel.locator("summary").click()
        return await panel.locator("p:visible").inner_text()
