# Run a native MCP app in a browser

This local counter shows two SDK features together. The official AI SDK client reads assistant text, the actual MCP tool arguments and result, and stream completion from `agents.ui.vercel_stream`. The official MCP Apps `AppBridge` then mounts the tool's HTML resource in a sandboxed iframe. Clicking **Increment** makes the iframe's `App` send `tools/call` through the same native `MCPApp` and updates its count.

The Responses model and MCP server are synthetic loopback fixtures. No API key or external service is required. This checks browser integration and native transport behavior; it does not test model quality or production authentication.

## Run

Use the source checkout and Python environment prepared by [CONTRIBUTING.md](../../../CONTRIBUTING.md). This example needs the native extension built from the same source, including the MCP app action binding. You also need Node 22 or later, npm, and `aiohttp` in that Python environment.

Run from the repository root:

```sh
uv pip install -r examples/codex/mcp_ui/requirements.txt
npm --prefix examples/codex/mcp_ui ci --ignore-scripts
npm --prefix examples/codex/mcp_ui run build
NO_PROXY=127.0.0.1,localhost uv run --no-sync python -m examples.codex.mcp_ui.server --port 8081
```

Open [the counter](http://127.0.0.1:8081) and click **Open counter**. The page shows `Counter opened.`, tool input `{"amount":1}`, a result with count `1`, and status `Complete`. The iframe shows count `1`. Click **Increment** inside the iframe to show count `2`. Restart the server to open another counter.

## Check the browser flow

Install the pinned Playwright browser once, then run the test from the repository root:

```sh
cd examples/codex/mcp_ui
npx playwright install chromium
cd ../../..
OPENAI_AGENTS_BROWSER_TESTS=1 NO_PROXY=127.0.0.1,localhost uv run --no-sync python -m pytest packages/codex/tests/test_mcp_ui_browser.py -q
```

The test needs a host that supports Chromium's sandbox. It does not disable the sandbox. A container that blocks Chromium user namespaces may need a supported browser-test host. Set `CHROMIUM_PATH` to use another compatible Chromium executable, or `NODE_BINARY` to select Node. The test runs only when `OPENAI_AGENTS_BROWSER_TESTS=1`. Once enabled, missing frontend assets, a missing browser, or a failed launch fails the test.

The browser test checks text, actual tool data, completion, the iframe's action response, rejection of an incorrect view token, and rejection of messages from another window.

## Local fixture boundaries

The server listens only on `127.0.0.1`. Visiting its page selects one synthetic user with an HTTP-only cookie. Browser actions must match that user, the page's origin, and the random token for the opened view. `MCPApp` permits only the originating `increment` tool. The fixture accepts only an increment of `1` and one model turn per process.

The server sets `include_tool_data=True` so the browser receives full tool arguments and results.

The iframe has `sandbox="allow-scripts"` without same-origin access. Its CSP permits only the local app script and denies network connections. The public script allows CORS so that the opaque iframe can load it. The official postMessage transport checks the sending window; only the host makes authenticated HTTP actions. Keep this example private. Replace its synthetic identity and fixed permissions before building a shared application.

Frontend dependencies are pinned in `package-lock.json`. `ai` consumes the UI stream; `@modelcontextprotocol/ext-apps` owns the iframe protocol. The SDK owns native tool execution. The example adds no competing client protocol.

The peer advertises `_meta.ui.resourceUri` in `tools/list` and returns ordinary text and structured tool data. `MCPApp.open` reads that exact HTML resource through the selected native connection. It captures current negotiated metadata when opened, rather than historical tool metadata; later remote metadata changes do not retarget the captured view. Without a declared URI, embedded or linked HTML results remain supported.
