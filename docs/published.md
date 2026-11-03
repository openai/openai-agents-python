# Build your first v1 agent

This page describes the v1 source preview. [Build and install from source](installation.md), then follow the [Agent and Session guide](sdk/index.md) and [matching examples](https://github.com/openai/openai-agents-python/blob/v1/examples/README.md). There is no public v1 wheel for this snapshot. For the published v0 SDK, use the [v0 documentation](https://openai.github.io/openai-agents-python/).

Define an `Agent`, then call `await agent.run(input)` for one answer. Use `async with agent.session() as session` for a conversation. Retain the Turn returned by `session.submit(...)` and await `turn.get_result()` to read its settled result. See [sessions and streaming](sdk/sessions.md) for the complete pattern.

## Set an OpenAI API key

Set your API key in the shell where you will run your agent.

On Linux or macOS:

```sh
export OPENAI_API_KEY="your-key"
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-key"
```

Save and run the [README example](https://github.com/openai/openai-agents-python/blob/v1/README.md#example) with the Python interpreter from your installed environment.
