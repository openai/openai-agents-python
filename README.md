# OpenAI Agents SDK

Build agents with the power and flexibility of the Codex harness. Define instructions, connect Python functions and MCP tools, and manage conversations in your application. Codex runs model turns and keeps conversation history. Python supplies typed tools, callbacks and application context.

## Build agents in Python

Start with Agents SDK for a new Python agent application. Connect your existing functions and application data. The SDK handles tool argument validation, function calls and typed result parsing.

Use [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) when you want OpenAI to run the agent and manage sessions and recovery.

## V1 source preview

This branch contains the native Codex-based Agents SDK v1. The initial public v1 preview requires a source build; there are no public v1 wheels or release tags for this snapshot. `pip install openai-agents` installs the published v0 SDK, which has a different API.

Existing v0 applications can keep using [v0.24.0](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0) and the [v0 documentation](https://openai.github.io/openai-agents-python/). Read the [migration guide](docs/sdk/migration.md) before porting. V1 does not convert v0 saved state, handoffs, guardrails, or tracing integrations automatically.

## Install

Follow the [source installation guide](docs/installation.md). You need CPython 3.10–3.14, Git, `uv`, Rust 1.95.0, and your platform's native build tools. The guide links the exact system prerequisites and commands. Build from the public `feat/codex-harness` branch, record the commit, and use documentation and examples from that same commit.

## Example

Save this as `agent.py` in your project directory:

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

Set an [OpenAI API key](docs/published.md#set-an-openai-api-key), then run the file with `.venv/bin/python agent.py` on Linux/macOS or `.\.venv\Scripts\python.exe agent.py` on Windows.

The script prints a greeting for Ada, such as `Hello, Ada!`. The model chooses the final wording, so it can vary. This example makes live model requests. The SDK calls your Python `greeting` function when the model requests that tool, then sends the tool's result back to the model.

## SDK guides

Start with the [SDK overview](docs/sdk/index.md), or choose a topic below. These guides describe this source branch.

| Guide | What you can do |
| --- | --- |
| [Agent configuration](docs/sdk/configuration.md) | Configure instructions, model, provider and typed output. |
| [Function tools](docs/sdk/tools.md#python-functions) | Make Python functions available with `function_tool`. |
| [MCP tools](docs/sdk/tools.md#native-mcp) | Connect an agent to tools from MCP servers. |
| [Sessions and state](docs/sdk/sessions.md) | Continue conversations and save settled state. |
| [Streaming](docs/sdk/sessions.md#stream-observations) | Show text and tool events as they arrive. |
| [API reference](docs/sdk/reference.md) | Find supported calls and defaults. |

## More examples

[Activate your installed environment](docs/installation.md#activate-the-environment), then run examples from the matching source checkout, for example `python examples/basic.py`. These examples call a model and need an [OpenAI API key](docs/published.md#set-an-openai-api-key).

- [Function tools](examples/basic.py): Call a Python greeting tool.
- [Typed application context](examples/typed_context.py): Return structured output using application context.
- [Conversation and streaming](examples/conversation.py): Stream a conversation and restore saved state.
- [Validated artifacts](examples/validated_artifact.py): Validate an artifact before writing it.
- [All examples](examples/README.md): Find workflow recipes and their setup requirements.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback.
