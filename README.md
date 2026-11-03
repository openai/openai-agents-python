# OpenAI Agents SDK

Build Python agents that use Codex to run model turns and keep conversation history. Define instructions, connect Python functions and MCP tools, and manage conversations in your application. Your Python code supplies typed tools, callbacks, and application data.

## Build agents in Python

Use Agents SDK to connect your Python functions and application data to an agent. The SDK validates tool arguments, calls your functions, and parses typed results.

Use [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) when you want OpenAI to run the agent and manage sessions and recovery.

## Install

Install [0.24.0-alpha.6](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.6) from a prebuilt Python wheel. Use standard CPython 3.10–3.14 and [create a virtual environment](docs/sdk/installation.md#create-a-python-environment), then run the command for your platform. The wheel includes the native runtime, so you do not need a compiler or Rust toolchain.

**Linux x86_64 (glibc 2.39 or newer)**

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.6/openai_agents-0.24.0a6-cp310-abi3-manylinux_2_39_x86_64.whl"
```

**macOS 11 or newer on Apple Silicon**

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.6/openai_agents-0.24.0a6-cp310-abi3-macosx_11_0_arm64.whl"
```

**Windows x64**

```powershell
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.6/openai_agents-0.24.0a6-cp310-abi3-win_amd64.whl"
```


This preview is distributed through GitHub Releases. The `openai-agents` package currently on PyPI is the previous SDK and does not provide the API shown here. See the [installation guide](docs/sdk/installation.md) for setup, supported platforms, and troubleshooting.

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

Set an [OpenAI API key](docs/published.md#set-an-openai-api-key), then run `python agent.py` in your activated environment.

This example makes live model requests and prints a greeting for Ada, such as `Hello, Ada!`. The wording can vary. When the model requests the `greeting` tool, the SDK calls your Python function and sends its result back to the model.

## SDK guides

Start with the [SDK overview](docs/sdk/index.md), or choose a topic below. These guides describe the SDK preview.

| Guide | What you can do |
| --- | --- |
| [Agent configuration](docs/sdk/configuration.md) | Set instructions, the model, the provider, and the output type. |
| [Function tools](docs/sdk/tools.md#python-functions) | Make Python functions available with `function_tool`. |
| [MCP tools](docs/sdk/tools.md#native-mcp) | Connect an agent to tools from MCP servers. |
| [Sessions and state](docs/sdk/sessions.md) | Continue conversations and save checkpoints. |
| [Streaming](docs/sdk/sessions.md#stream-observations) | Show text and tool events as they arrive. |
| [API reference](docs/sdk/reference.md) | Find supported calls and defaults. |

## More examples

[Activate your Python environment](docs/sdk/installation.md#create-a-python-environment), then run examples from the same release as your installed wheel, for example `python examples/basic.py`. These examples call a model and need an [OpenAI API key](docs/published.md#set-an-openai-api-key).

- [Function tools](examples/basic.py): Call a Python greeting tool.
- [Typed application context](examples/typed_context.py): Return structured output using application context.
- [Conversation and streaming](examples/conversation.py): Stream a conversation and restore saved state.
- [Validated artifacts](examples/validated_artifact.py): Validate an artifact before writing it.
- [All examples](examples/README.md): Find workflow recipes and their setup requirements.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback.

## Contributing

For local SDK development, source builds, and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).
