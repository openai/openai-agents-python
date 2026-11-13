# Quickstart

<a id="build-agents-with-codex"></a>

Build an agent that calls a Python function and returns a greeting. You define its instructions and tools. The Agents SDK uses Codex to coordinate model requests and tool calls inside your application.

## Before you begin

1. Follow the [installation guide](installation.md) to install a preview wheel and activate your Python environment.
2. [Create an OpenAI API key](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key) for the live example below.

These guides use the alpha.7 wheel as the installation baseline and mark features that require a newer development build. See [releases and documentation](versions.md) when choosing examples or upgrading.

The example below needs an OpenAI API key and makes live model requests that can incur charges. To test with scripted responses and no API key, use the [testing guide](testing.md).

## Run your first agent

### 1. Create the agent

Save this script as `agent.py` in your project directory. The `@function_tool` decorator makes `greeting` available to the model and describes its arguments from the Python type annotations.

```python
import asyncio

from agents import Agent, function_tool


@function_tool
async def greeting(name: str) -> str:
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Use greeting to greet the user.",
        tools=[greeting],
    )
    result = await agent.run("Greet Ada.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

### 2. Set your API key and run the script

Replace `your-key` with your OpenAI API key. In the same shell, with your Python environment activated, run:

```sh
export OPENAI_API_KEY="your-key"
python agent.py
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-key"
python agent.py
```

### 3. Check the result

The script prints a greeting for Ada, such as `Hello, Ada!`. When the model requests the `greeting` tool, the SDK calls your Python function and returns its result to the model. The model chooses the final wording, so it can vary.

Importing `agents` or declaring the agent and tools does not start work. The `agent.run()` call opens a Session, waits for the result, and closes the Session. With the default in-memory storage, calling `agent.run()` again without a saved checkpoint starts an independent conversation.

Use `agent.session()` to keep history between messages or stream progress. In synchronous code, use `agent.run_sync()` outside an existing event loop. See [core concepts](concepts.md) for the relationship between Agent, Session, and Turn.

## Choose your next task

| Task | Guide |
| --- | --- |
| Call Python code with application data | [Function tools](tools.md#python-functions) and [context](tools.md#application-context) |
| Connect a tool server | [MCP servers](mcp.md) |
| Continue a conversation | [Conversations](sessions.md) |
| Display progress as it arrives | [Streaming and events](streaming.md) |
| Save a conversation and resume later | [Save and restore state](state.md) |
| Return a validated Python value | [Structured output](outputs.md) |
| Choose a model or supply credentials | [Models and authentication](models.md) |
| Test without a model service | [Testing](testing.md) |
| Diagnose failures or set a timeout | [Troubleshooting](troubleshooting.md) |

Find worked examples for [images, retrieval, application memory, and specialist agents](recipes.md). Use the [API reference](reference.md) to look up calls and defaults.

For an existing v0 application, start with the [runtime comparison](concepts.md#compare-with-v0240) and [migration guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/MIGRATION.md).
