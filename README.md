# OpenAI Agents SDK

Build Python agents that call your functions, use MCP tools, and keep a conversation across messages. Define the agent's instructions and tools; the SDK uses Codex to coordinate model requests and tool calls. It validates tool arguments, calls your functions, and parses typed results.

Your application runs the agent and owns its workers, storage, and credentials. See [how the SDK and hosted Agents API run agents](docs/sdk/choosing.md).

## Install

Install a prebuilt preview wheel from GitHub Releases. Follow the [installation guide](docs/sdk/installation.md) to set up a Python environment and find the command for your platform.

<a id="example"></a>

## Run your first agent

With your Python environment active, save this script as `agent.py` in your project directory. It needs an OpenAI API key and makes live model requests that can incur charges.

```python
import asyncio

from agents import Agent, function_tool


@function_tool
async def greeting(name: str) -> str:
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions=(
            "Use greeting to greet the user. "
            "After the tool returns, repeat its greeting in your final answer."
        ),
        tools=[greeting],
    )
    result = await agent.run("Greet Ada.")
    print(result.final_output)


asyncio.run(main())
```

Set `OPENAI_API_KEY` in your environment, then run `python agent.py` in the same shell. See how to [create and export an API key](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key).

The script prints a greeting for Ada. The wording can vary. The `@function_tool` decorator tells the model which arguments `greeting` takes. The SDK calls the function when the model requests it.

## Guides and examples

Find the [guides and examples for your installed version](docs/sdk/versions.md), including a test you can run without an API key.

## Existing v0 applications

You can stay on v0. When you choose to upgrade, we recommend [migrating directly to native v1](MIGRATION.md), including your tools, execution, and state handling. The guide includes an API mapping, validation steps, and instructions for using a coding agent to help with the port.

If a workflow needs the new package before you can port it, this branch provides `agents.classic` as an intermediate step. It retains the Python runner while the root `agents` API uses Codex. See [classic setup](CLASSIC.md) for imports, dependencies, and compatibility limits.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback.

## Contributing

To build or test the SDK from source, follow the [contributor guide](CONTRIBUTING.md). Use the [development guides](docs/sdk/index.md) and [examples](examples/codex/README.md) from the same commit as your build.
