import assert from 'node:assert/strict';
import { URL } from 'node:url';
import { chromium } from 'playwright';
const url = process.argv[2];
if (!url)
  throw new Error('Usage: node browser-check.mjs http://127.0.0.1:PORT');
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_PATH
    ? { executablePath: process.env.CHROMIUM_PATH }
    : {}),
  chromiumSandbox: true,
});
try {
  const page = await browser.newPage();
  const errors = [];
  let actionRequests = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      new URL(request.url()).pathname === '/api/action'
    )
      actionRequests++;
  });
  page.setDefaultTimeout(12000);
  page.on('console', (message) => {
    if (message.type() === 'error') console.error(message.text());
  });
  page.on('response', (response) => {
    if (response.status() >= 400)
      console.error(response.status(), response.url());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(url);
  await page.getByRole('button', { name: 'Open counter', exact: true }).click();
  await page.waitForFunction(
    () =>
      document.getElementById('status').textContent === 'Complete' ||
      document.getElementById('error').textContent,
  );
  assert.equal(await page.locator('#error').innerText(), '');
  await page.waitForFunction(() =>
    document.getElementById('tools').textContent.includes('output-available'),
  );
  assert.match(await page.locator('#text').innerText(), /Counter opened/);
  const tools = JSON.parse(await page.locator('#tools').innerText());
  assert.equal(tools.length, 1);
  assert.equal(tools[0].input.amount, 1);
  assert.equal(tools[0].output.structuredContent.count, 1);
  assert.ok(tools[0].toolCallId);
  const frame = page.frameLocator('#app');
  await frame.locator('#count').filter({ hasText: /^1$/ }).waitFor();
  await frame.getByRole('button', { name: 'Increment', exact: true }).click();
  await frame.locator('#count').filter({ hasText: /^2$/ }).waitFor();
  assert.equal(await frame.locator('#error').innerText(), '');
  assert.equal(await page.locator('#error').innerText(), '');
  assert.deepEqual(errors, []);
  const security = await page.evaluate(async () => {
    const wrongView = await fetch('/api/action', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-MCP-Example': '1',
        'X-View': 'wrong',
      },
      body: JSON.stringify({ name: 'increment', arguments: { amount: 1 } }),
    });
    return wrongView.status;
  });
  assert.equal(security, 403);
  const cookies = await page.context().cookies();
  const badOrigin = await fetch(url + '/api/action', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Origin: 'http://untrusted.invalid',
      'X-MCP-Example': '1',
      'X-View': 'wrong',
      Cookie: cookies.map((c) => `${c.name}=${c.value}`).join('; '),
    },
    body: JSON.stringify({ name: 'increment', arguments: { amount: 1 } }),
  });
  assert.equal(badOrigin.status, 403);
  assert.match(await badOrigin.text(), /Unexpected action origin/);
  // A distinct window cannot send tools/call through this iframe's transport.
  const actionsBeforeForeignMessage = actionRequests;
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        const received = (event) => {
          if (event.source === window && event.data?.id === 999) {
            window.removeEventListener('message', received);
            resolve();
          }
        };
        window.addEventListener('message', received);
        window.postMessage(
          {
            jsonrpc: '2.0',
            id: 999,
            method: 'tools/call',
            params: { name: 'increment', arguments: { amount: 1 } },
          },
          '*',
        );
      }),
  );
  await frame.getByRole('button', { name: 'Increment', exact: true }).click();
  await frame.locator('#count').filter({ hasText: /^3$/ }).waitFor();
  assert.equal(
    actionRequests - actionsBeforeForeignMessage,
    1,
    'Only the subsequent iframe action may reach /api/action',
  );
  console.log(
    JSON.stringify({
      text: 'Counter opened.',
      input: { amount: 1 },
      initialCount: 1,
      iframeCount: 2,
      terminal: 'Complete',
      wrongView: 403,
      foreignWindowIgnored: true,
      badOrigin: 403,
      afterForeignWindowCount: 3,
    }),
  );
} finally {
  await browser.close();
}
