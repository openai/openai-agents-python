# OpenAI Agents SDK

Build Python agents that use Codex to run model turns and keep conversation history. Define instructions, connect Python functions and MCP tools, and manage conversations in your application. The SDK validates tool arguments, calls your functions, and parses typed results.

Your application runs the agent and owns its workers, storage, and credentials. See [Choose how to run your agent](docs/sdk/choosing.md) to compare this model with the hosted Agents API.

## Existing v0 applications

The source on this branch also provides `agents.classic` for existing v0 workflows. It retains the Python runner while the root `agents` API uses Codex. This addition is not included in the preview release linked below. See [classic migration](CLASSIC.md) for source installation, import changes, dependency extras, and compatibility limits.

To move a supported Classic Agent to native execution, use the experimental [SDKRunner migration adapter](MIGRATION.md). It reuses your Classic Model and function tools within an explicit, limited contract. It also requires this source; the preview release below does not contain it.

## Install

The commands below install the published preview. Use [documentation and example files from that release](docs/sdk/versions.md); these branch guides also describe APIs added after the release.

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

<a id="example"></a>

## Run your first agent

With the SDK installed, save this script as `agent.py` in your project directory. It makes live model requests and can incur charges. For a test that needs no API key, use the [scripted testing guide](docs/sdk/testing.md) for your SDK version.

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

The example prints a greeting for Ada, such as `Hello, Ada!`. The wording can vary. When the model requests the `greeting` tool, the SDK calls your Python function and sends its result back to the model.

## SDK guides

The [quickstart](docs/sdk/index.md) explains the first run and links to the next tasks. Use [core concepts](docs/sdk/concepts.md) to understand Agent, Session and Turn, or choose a task below. Follow the [guides for your installed version](docs/sdk/versions.md).

| Guide | What you can do |
| --- | --- |
| [Agent configuration](docs/sdk/configuration.md) | Set instructions, the model, the provider, and the output type. |
| [Tools](docs/sdk/tools.md) | Call Python functions and select tools for each turn. |
| [MCP servers](docs/sdk/mcp.md) | Connect servers, configure approval, and inspect tool results. |
| [Conversations and streaming](docs/sdk/sessions.md) | Continue a conversation, show progress, and stop work. |
| [Save and restore state](docs/sdk/state.md) | Choose storage, compact history, and restore checkpoints. |
| [Command execution](docs/sdk/execution.md) | Run commands with explicit workspace and network permissions. |
| [Approvals and manual results](docs/sdk/approvals.md) | Ask for a decision or accept a tool result from your application. |
| [Delegate work](docs/sdk/delegation.md) | Start and control native child agents. |
| [Usage and tracing](docs/sdk/observability.md) | Read token usage, export telemetry, and diagnose outcomes. |
| [API reference](docs/sdk/reference.md) | Find supported calls and defaults. |

## More examples

Follow the [example setup](docs/sdk/installation.md#examples-and-upgrades) to download the matching source archive. In this checkout, run `python packages/codex/examples/basic.py` from the repository root. The four examples listed below call a live model and need an [OpenAI API key](docs/published.md#set-an-openai-api-key). The full example index also includes no-key tests and local service examples.

- [Function tools](packages/codex/examples/basic.py): Call a Python greeting tool.
- [Typed application context](packages/codex/examples/typed_context.py): Return structured output using application context.
- [Conversation and streaming](packages/codex/examples/conversation.py): Stream a conversation and restore saved state.
- [Validated artifacts](packages/codex/examples/validated_artifact.py): Validate an artifact before writing it.
- [All examples](packages/codex/examples/README.md): Find workflow recipes and their setup requirements.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback.

## Contributing

For local SDK development, source builds, and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).
