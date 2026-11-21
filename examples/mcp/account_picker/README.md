# Choose an account from an MCP resource

Choose a synthetic account in a desktop dropdown. Return its authorized alias to the paused SDK tool call. No API key, web server, or real accounts.

## Try the picker

Use a graphical desktop and Python with Tk. Install the repository dependencies with `make sync`. Run this command from the repository root.

```sh
uv run python -m examples.mcp.account_picker.main --pick
```

1. Select **Savings** under **Demo account**.
2. Choose **Use account**. Look for `Savings` in the terminal.

To cancel without approving the call, close the window. To test without a display, run the following command.

```sh
uv run pytest -q tests/mcp/test_account_picker_example.py
```

## Resource discovery and reads

- **[Source](server.py)** — One fixed demo user on a private stdio connection. One plain-text resource with a permitted alias on each line.
- **[Application](main.py)** — Use `MCPServerStdio.list_resources()` to find the descriptor. Match the URI, MIME type, and `_meta` before calling `read_resource()`. Check the returned URI, content type, and labels before display.
- **UI metadata** — Keep the descriptor on `Picker.resource`. Treat `example.com/widget` as a local widget identifier with no authority to grant access. Keep the descriptor and raw body out of agent instructions and tool history.

## Rendering, approval, and authorization

1. **Pause.** Use a scripted agent to call `choose_account` with no arguments. Require SDK approval before execution.
2. **Render.** Create a fresh `Picker` for the conversation and interrupted call ID. Show only preapproved labels in the application's read-only Tk dropdown. Never accept free text or account numbers.
3. **Submit.** Handle **Use account** in the app. Accept the exact request object once. Expire the callback on leaving the dialog, even after cancellation.
4. **Resume.** Approve only that interruption and resume its in-memory run state. In the tool, check the call ID. Ask the source to confirm the alias against current grants, even after a successful read. Return only the confirmed alias.

## Limits

- **Trust** — Bundled source and local application only. For multiple users, authenticate each user at the source. Check access on every read and selection. Keep real account numbers out of tool output and logs.
- **Lifetime** — One process and a blocking desktop dialog. No cross-process callbacks or durable approvals.
- **MCP Apps** — Raw resource reads only. No HTML or JavaScript rendering, iframe sandbox, host messaging, or automatic widget discovery. Example-specific metadata key and label format.
