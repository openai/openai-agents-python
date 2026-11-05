# Build agents with Codex

Build an agent that calls a Python function and returns a greeting. You define its instructions and tools; the Agents SDK uses Codex to run the model and tool loop in your application.

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

On Windows PowerShell, use `$env:OPENAI_API_KEY = "your-key"`, then `python agent.py`.

The script prints a greeting for Ada, such as `Hello, Ada!`. When the model requests the `greeting` tool, the SDK calls your Python function and returns its result to the model. The model chooses the final wording, so it can vary.

`agent.run()` opens a Session, waits for the result, and closes the Session. Use `agent.session()` when you need to continue the conversation or stream events. In synchronous code, use `agent.run_sync()` outside an existing event loop. The [core concepts guide](concepts.md) explains these interfaces and how the runtime fits into your application.

## Choose your next task

| Task | Guide |
| --- | --- |
| Understand Agent, Session and Turn | [Core concepts](concepts.md) |
| Call Python code with application data | [Function tools](tools.md#python-functions) and [context](tools.md#application-context) |
| Continue a conversation or stream its answer | [Sessions](sessions.md) and [streaming](sessions.md#stream-observations) |
| Save a conversation and resume later | [Save and restore state](state.md) |
| Return a validated Python value | [Structured output](configuration.md#structured-output) |
| Run commands in a workspace | [Command execution](execution.md) |
| Ask for approval or supply a tool result | [Approvals and manual results](approvals.md) |
| Delegate work to child agents | [Delegation](delegation.md) |
| Test without a model service | [Testing](testing.md) |
| Diagnose failures or set a timeout | [Troubleshooting](troubleshooting.md) |
| Inspect usage and export telemetry | [Usage](troubleshooting.md#read-usage-without-double-counting) and [telemetry](configuration.md#telemetry) |

Find worked examples for [images, retrieval, application memory, and specialist agents](recipes.md). Before combining features, check the [limits on storage, approvals, and delegation](choosing.md#choose-feature-combinations). Use the [API reference](reference.md) to look up signatures and defaults.
