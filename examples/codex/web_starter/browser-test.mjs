// Real browser + actual HTTP; keep all records and credentials synthetic.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
const browser = await chromium.launch({headless: true, chromiumSandbox: true});
const context = await browser.newContext();
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const origin = process.argv[2];
try {
  await page.goto(origin);
  await page.locator('#login').click();
  await page.getByText('Signed in as alice', {exact: true}).waitFor();
  await page.locator('#composer button').click();
  await page.getByRole('button', {name: 'Approve', exact: true}).waitFor();
  assert.match(await page.locator('#card').innerText(), /10000 rows/);
  await page.locator('#disconnect').click();
  await page.getByText('Reader disconnected. Work continues.', {exact: true}).waitFor();
  await page.reload();
  await page.locator('#login').click();
  await page.getByRole('button', {name: 'Approve', exact: true}).waitFor();
  await page.getByRole('button', {name: 'Approve', exact: true}).click();
  await page.getByText('completed', {exact: true}).waitFor();
  assert.match(await page.locator('#final').innerText(), /Report complete/);
  const href = await page.getByRole('link', {name: 'Download validated XML'}).getAttribute('href');
  const artifact = await context.request.get(origin + href);
  assert.equal(await artifact.text(), '<presentation><slide>Report</slide></presentation>');
  const foreign = await browser.newContext();
  const other = await foreign.newPage();
  await other.goto(origin);
  await other.locator('#user').selectOption('bob');
  await other.locator('#login').click();
  await other.getByText('Signed in as bob', {exact: true}).waitFor();
  assert.equal((await foreign.request.get(origin + href)).status(), 404);
  await foreign.close();
  const completedReceipt = await page.evaluate(() => localStorage.getItem('web-starter:alice'));
  const completedState = await (await context.request.get(`${origin}/api/receipts/${completedReceipt}`)).json();
  await page.locator('#composer button').click();
  await page.getByRole('button', {name: 'Approve', exact: true}).waitFor();
  const nextReceipt = await page.evaluate(() => localStorage.getItem('web-starter:alice'));
  const nextState = await (await context.request.get(`${origin}/api/receipts/${nextReceipt}`)).json();
  assert.notEqual(nextReceipt, completedReceipt);
  assert.ok(nextState.revision < completedState.revision);
  // A Stop reply can arrive after SSE has already rendered terminal cleanup.
  const stoppedReceipt = await page.evaluate(() => localStorage.getItem('web-starter:alice'));
  const stopUrl = `${origin}/api/receipts/${stoppedReceipt}/stop`;
  let releaseStop, sawStop, stopState;
  const stopped = new Promise(resolve => { sawStop = resolve; });
  const stopRelease = new Promise(resolve => { releaseStop = resolve; });
  await page.evaluate(() => {
    const fetch = window.fetch;
    window.fetch = async (...args) => {
      const response = await fetch(...args);
      if (String(args[0]).endsWith('/stop')) {
        const json = response.json.bind(response);
        response.json = async () => {
          const state = await json();
          // The timer runs after the client's awaiting JSON/render microtasks.
          setTimeout(() => { window.stopResponseHandled = true; }, 0);
          return state;
        };
      }
      return response;
    };
  });
  await page.route(stopUrl, async route => {
    const response = await route.fetch();
    stopState = await response.json(); sawStop(); await stopRelease;
    await route.fulfill({response});
  });
  await page.locator('#stop').click();
  await stopped;
  assert.equal(stopState.status, 'stopping');
  await page.getByText('cancelled', {exact: true}).waitFor();
  const terminalState = await (await context.request.get(`${origin}/api/receipts/${stoppedReceipt}`)).json();
  assert.ok(terminalState.revision > stopState.revision);
  releaseStop();
  await page.waitForFunction(() => window.stopResponseHandled === true);
  assert.equal(await page.locator('#status').innerText(), 'cancelled');
  assert.equal(await page.getByRole('button', {name: 'Approve', exact: true}).count(), 0);
  assert.equal(await page.locator('#final').innerText(), 'Final result: cancelled');
  await page.unroute(stopUrl);
  // Reconnect accepts the unchanged terminal snapshot at the same revision.
  await page.locator('#disconnect').click();
  await page.locator('#reconnect').click();
  await page.getByText('cancelled', {exact: true}).waitFor();
  assert.equal(await page.getByRole('link', {name: 'Download validated XML'}).count(), 0);
  // A delayed GET failure from a disconnected reader cannot overwrite its UI.
  const aliceReceipt = await page.evaluate(() => localStorage.getItem('web-starter:alice'));
  let releaseGet, sawGet;
  const got = new Promise(resolve => { sawGet = resolve; });
  const getRelease = new Promise(resolve => { releaseGet = resolve; });
  const receiptUrl = `${origin}/api/receipts/${aliceReceipt}`;
  await page.route(receiptUrl, async route => {
    sawGet(); await getRelease;
    await route.fulfill({status: 500, contentType: 'text/plain', body: 'synthetic-old-reader-failure'});
  });
  await page.locator('#reconnect').click();
  await got;
  await page.locator('#disconnect').click();
  releaseGet();
  await page.waitForResponse(response => response.url() === receiptUrl);
  assert.equal(await page.locator('#error').innerText(), '');
  assert.equal(await page.locator('#status').innerText(), 'Reader disconnected. Work continues.');
  await page.unroute(receiptUrl);
  // Delay an accepted Alice POST until after Bob signs in. Its late response
  // must not render Alice's prompt or change Bob's selected receipt/storage.
  let releaseSubmit, sawSubmit, delayedReceipt;
  const submitted = new Promise(resolve => { sawSubmit = resolve; });
  const release = new Promise(resolve => { releaseSubmit = resolve; });
  await page.route('**/api/receipts', async route => {
    const response = await route.fetch(); delayedReceipt = (await response.json()).id; sawSubmit(); await release;
    await route.fulfill({response});
  });
  await page.locator('#prompt').fill('Alice private delayed prompt');
  await page.locator('#composer button').click();
  await submitted;
  await page.locator('#user').selectOption('bob');
  await page.locator('#login').click();
  await page.getByText('Signed in as bob', {exact: true}).waitFor();
  const bobReceipt = await page.evaluate(() => localStorage.getItem('web-starter:bob'));
  releaseSubmit();
  await page.waitForResponse(response => response.url().endsWith('/api/receipts'));
  await page.waitForFunction(() => !document.querySelector('#composer button').disabled);
  assert.equal(await page.locator('#request').innerText(), '');
  assert.equal(await page.evaluate(() => localStorage.getItem('web-starter:bob')), bobReceipt);
  assert.notEqual(await page.evaluate(() => localStorage.getItem('web-starter:alice:pending')), null);
  await page.unroute('**/api/receipts');
  // Release Alice's synthetic worker before checking Bob's lower-revision receipt.
  const alice = await browser.newContext();
  await alice.request.post(`${origin}/api/login`, {headers: {'X-Web-Starter': '1'}, data: {user: 'alice'}});
  await alice.request.post(`${origin}/api/receipts/${delayedReceipt}/stop`, {headers: {'X-Web-Starter': '1'}, data: {}});
  await alice.close();
  await page.locator('#prompt').fill('Bob lower revision prompt');
  await page.locator('#composer button').click();
  await page.getByRole('button', {name: 'Approve', exact: true}).waitFor();
  assert.equal(await page.locator('#request').innerText(), 'Bob lower revision prompt');
  const bobState = await (await context.request.get(`${origin}/api/receipts/${await page.evaluate(() => localStorage.getItem('web-starter:bob'))}`)).json();
  assert.ok(bobState.revision < terminalState.revision);
  assert.deepEqual(errors, []);
  assert.equal(await page.locator('#error').innerText(), '');
  process.stdout.write(JSON.stringify({browser: 'chromium', client: 'ai@6.0.315',
    verified: ['native-tools', 'typed-final', 'approval', 'disconnect', 'reload', 'authorized-download', 'foreign-denial', 'stop', 'stop-response-ordering', 'equal-revision-reconnect', 'lower-revision-receipt-owner', 'stale-submit-isolation', 'stale-reader-error-isolation']}));
} finally { await context.close(); await browser.close(); }
