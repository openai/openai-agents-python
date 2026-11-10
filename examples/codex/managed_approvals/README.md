# Managed function approvals

Use this example with the matching native SDK preview source and wheel. From the repository root, run `uv run python examples/codex/managed_approvals/main.py`. It uses synthetic data and a loopback Responses fixture, so it needs no provider credentials. Keep loopback traffic off your proxy. The expected output is `Approved callback ran once.`

Set `@function_tool(needs_approval=True)` to pause before a Python function runs. Read `ApprovalRequested` from `session.interactions()`, inspect `arguments` and `effective_arguments`, then await `approve()` or `reject(reason)`. Approval runs the registered callback. Rejection returns a failed tool result without calling it. The request belongs to its original Session, Turn, and call; a second decision or an expired request fails.

`needs_approval` also accepts a synchronous or asynchronous predicate receiving `ToolContext`. A false result runs the function without asking the application. By default, the input guard runs after approval, immediately before the callback. With `run_before_approval=True`, the input guard also runs before the approval predicate. A replacement from that first check becomes the request's effective input. The guard runs again before execution and can still reject or replace that input. Output guards run only after callback execution.

Use `approve(always=True)` or `reject(reason, always=True)` to remember a decision in the current Session. The decision is scoped to the captured function declaration and Agent binding. It does not carry into a new Session or grant permission to another declaration with the same name. Input guards still run before execution.

To pause for another process, await `session.suspend()` while approval is pending and save the returned `SessionState` securely. Reopen with `agent.session(state=state)` and current trusted function declarations. The saved pending requirement still needs a decision even if the current predicate returns false. Accepted sibling results are retained. Restore never serializes a callback or grants authority from conversation history. Portable snapshots do not coordinate independent writers or make uncertain effects safe to retry.

Without an interaction handler, one-shot `agent.run()` raises `InteractionRequired` with its portable `state` before a required callback runs. An explicit Session lets you decide while retaining its original Turn. Live Realtime function calls use the same approval decisions and guard checks; closing the voice operation expires its pending request.

This slice supports bare managed Python function tools. It does not add managed MCP, nested guarded restore, namespaced or generated-code approvals, Computer approvals, or persistent-store composition. `manual_tool()` remains the separate API for supplying an application result rather than approving an SDK callback. Approval cannot roll back an effect after the callback starts; cancellation waits for callback cleanup.
