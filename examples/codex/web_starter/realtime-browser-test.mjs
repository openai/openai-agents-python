import assert from "node:assert/strict";
import { chromium } from "playwright";

// This witness requires a host that supports Chromium's normal sandbox.
const browser = await chromium.launch({
    headless: true,
    chromiumSandbox: true,
    ...(process.env.CHROMIUM_PATH
        ? { executablePath: process.env.CHROMIUM_PATH }
        : {}),
    args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ],
});
try {
    const context = await browser.newContext({ permissions: ["microphone"] });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(process.argv[2]);
    await page.getByRole("button", { name: "Connect" }).click();
    const observed = await page.evaluate(async () => await window.acceptance);
    assert.equal(observed.ok, true, JSON.stringify(observed));
    assert.ok(observed.result.microphoneBytes > 0);
    assert.ok(observed.result.playbackTime > 0);
    assert.equal(observed.result.closed, true);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify(observed.result));
} finally {
    await browser.close();
}
