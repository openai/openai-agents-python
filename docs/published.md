# Run the SDK preview

[Install the SDK preview](sdk/installation.md), then choose the [guides for your installed release](sdk/versions.md). The [quickstart](sdk/index.md) shows a complete first agent. To run repository examples, download the matching source archive as described in [example setup](sdk/installation.md#examples-and-upgrades).

Examples that call a live model need an OpenAI API key. The [scripted testing guide](sdk/testing.md) runs without one.

## Set an OpenAI API key

[Create an OpenAI API key](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key) if you do not already have one. Replace `your-key` below with that key, and set it in the shell where you run your agent.

On Linux or macOS:

```sh
export OPENAI_API_KEY="your-key"
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-key"
```

Save the [quickstart script](sdk/index.md#run-your-first-agent) as `agent.py`, then run `python agent.py` with the Python interpreter from your installed environment. Use the quickstart from your installed release.
