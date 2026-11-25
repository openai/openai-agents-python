"""Playwright adapter for trusted offline FAQ pages."""

from playwright.async_api import Browser


async def read_panel(instance: Browser, *, html: str, label: str) -> str:
    """Open one collapsed details panel by its test ID and read its visible paragraph."""
    async with await instance.new_context(
        offline=True, java_script_enabled=False, accept_downloads=False
    ) as context:
        context.set_default_timeout(5000)
        page = await context.new_page()
        await page.set_content(html)
        panel = page.get_by_test_id(label)
        await panel.locator("summary").click()
        return await panel.locator("p:visible").inner_text()
