# Build agents with Codex

Build an agent that calls a Python function and returns a greeting. The Agents SDK uses Codex to run the agent in your application.

See [Choose how to run your agent](choosing.md) for deployment choices and supported feature combinations.

Define an `Agent` and a tool with `@function_tool`. Use `await agent.run(input)` for one answer. Use `agent.session()` to continue a conversation, stream events, or save the conversation.

## Before you begin

Follow the [installation guide](installation.md) to install a preview wheel and activate your Python environment.

The example below needs an OpenAI API key and makes live model requests. To test with scripted responses and no API key, use the [testing guide](testing.md).

## Run your first agent

Importing `agents` or declaring agents and tools does not start a model request or execute a tool. Save this script as `agent.py` in your project directory:

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

Set an OpenAI API key in your environment before running the script:

```sh
export OPENAI_API_KEY="your-key"
python agent.py
```

On Windows PowerShell, use `$env:OPENAI_API_KEY = "your-key"`, then `python agent.py`. In a synchronous application, use `agent.run_sync` outside an existing event loop. The [basic example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/basic.py) shows the same complete pattern.

The script prints a greeting for Ada, such as `Hello, Ada!`. The model chooses the final wording, so it can vary. The model can request a call to the `greeting` tool. The SDK calls your Python function and returns its result to the model.

Continue with [Python functions](tools.md#python-functions), [application context](tools.md#application-context), [conversation and streaming](sessions.md), [checkpoints](sessions.md#compact-save-and-restore), and [native MCP](tools.md#native-mcp). See [configuration](configuration.md) when you need to change native defaults.

## Choose your next task

| I want to… | Start here |
| --- | --- |
| Understand Agent, Session and Turn | [Core concepts](concepts.md) |
| Call Python code with application data | [Function tools](tools.md#python-functions) and [context](tools.md#application-context) |
| Continue a conversation or stream its answer | [Sessions](sessions.md) and [streaming](sessions.md#stream-observations) |
| Save a conversation and resume later | [Choose a persistence method](sessions.md#choose-how-to-store-a-conversation) |
| Return a validated Python value | [Structured output](configuration.md#structured-output) |
| Test without a model service | [Testing](testing.md) |
| Diagnose failures or set a timeout | [Troubleshooting](troubleshooting.md) |
| Inspect usage and export telemetry | [Usage](troubleshooting.md#read-usage-without-double-counting) and [telemetry](configuration.md#telemetry) |

Find worked examples for [images, retrieval, application memory, approvals, tools, and specialist Agents](recipes.md). Before combining features, check the [limits on saving, restoring, and delegation](choosing.md#choose-feature-combinations).

## How the runtime fits

The [core concepts guide](concepts.md#how-the-runtime-fits) shows how your Python application, embedded Codex, model provider and tools fit together. Start there before configuring native children or remote execution.

## Test without a model service

The [testing guide](testing.md) runs real Python tools and the native Session against scripted model responses. It includes a complete script that needs no API key, its expected output, and a pytest command.

## More reference

See the [API reference](reference.md) for supported calls and the [troubleshooting guide](troubleshooting.md) for common symptoms.
