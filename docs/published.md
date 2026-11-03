# Build your first v1 agent

Follow the [installation guide](sdk/installation.md) to set up the SDK preview. Then use the [quickstart](sdk/index.md) and [examples](../examples/README.md) to build your application.

Define an `Agent`, then call `await agent.run(input)` for one answer. Use `async with agent.session() as session` for a conversation. Keep the `Turn` returned by `session.submit(...)`. Await `turn.get_result()` to read the completed result. See [sessions and streaming](sdk/sessions.md) for the complete pattern.

## Set an OpenAI API key

Set your API key in the shell where you run your agent.

On Linux or macOS:

```sh
export OPENAI_API_KEY="your-key"
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-key"
```

Save and run the [README example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/README.md#example) with the Python interpreter from your installed environment.
