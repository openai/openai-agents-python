# Block a tool when a pre-execution hook fails

Codex 0.166.0 reports a failed `PreToolUse` hook but can still start the tool. Python can receive the failure too late to prevent a write.

[This patch](03-required-tool-hooks.patch) blocks the call in Codex before dispatch if any synchronous `PreToolUse` handler fails or times out. It preserves hook events and leaves sandbox and approval checks in place. It covers other configured synchronous handlers as well as the SDK relay. Asynchronous notifications and output hooks cannot prevent an action that has already started.

After an approval pause, the SDK reconnects the input hook relay and runs configured start callbacks on the replacement worker. It keeps the approved input without applying `prepare_tool_input` again.

Follow the [wheel verification steps](README.md#test-a-change). With the installed wheel, check that a failing or unresponsive relay leaves a test file untouched and that a successful decision allows the edit. Repeat after restarting at an approval. Remove this patch when the pinned version of Codex blocks these calls on its own.
