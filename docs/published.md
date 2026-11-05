# Run the SDK preview

Follow the [installation guide](sdk/installation.md) to set up the SDK preview. Then use the [quickstart](sdk/index.md) and [examples](../examples/README.md) to build your application.

Examples that call a live model need an OpenAI API key. The [scripted testing guide](sdk/testing.md) runs without one.

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

Save and run the [README example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/README.md#run-your-first-agent) with the Python interpreter from your installed environment.
