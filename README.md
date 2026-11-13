# OpenAI Agents SDK

Build Python agents that call your functions, use MCP tools, and keep a conversation across messages. Define the agent's instructions and tools; the SDK uses Codex to coordinate model requests and tool calls. It validates tool arguments, calls your functions, and parses typed results.

Your application runs the agent and owns its workers, storage, and credentials. See [Choose how to run your agent](docs/sdk/choosing.md) to compare this model with the hosted Agents API.

## Install

The commands below install the published preview. Use [documentation and example files from that release](docs/sdk/versions.md); branch guides can change between releases.

Install [0.24.0-alpha.7](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.7) from a prebuilt Python wheel. Use standard CPython 3.10–3.14 and [create a virtual environment](docs/sdk/installation.md#create-a-python-environment), then run the command for your platform. The wheel includes the native runtime, so you do not need a compiler or Rust toolchain.

**Linux x86_64 (glibc 2.39 or newer)**

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.7/openai_agents-0.24.0a7-cp310-abi3-manylinux_2_39_x86_64.whl"
```

**macOS 11 or newer on Apple Silicon**

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.7/openai_agents-0.24.0a7-cp310-abi3-macosx_11_0_arm64.whl"
```

**Windows x64**

```powershell
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v0.24.0-alpha.7/openai_agents-0.24.0a7-cp310-abi3-win_amd64.whl"
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

Set an [OpenAI API key](docs/sdk/index.md#2-set-your-api-key-and-run-the-script), then run `python agent.py` in your activated environment.

The example prints a greeting for Ada, such as `Hello, Ada!`. The wording can vary. The `@function_tool` decorator exposes the function's name and typed arguments to the model. When the model requests `greeting`, the SDK calls your Python function and sends its result back to the model.

## SDK guides

The [quickstart](docs/sdk/index.md) explains the first run and links to the next tasks. Use [core concepts](docs/sdk/concepts.md) to understand Agent, Session and Turn, or choose a task below. Follow the [guides for your installed version](docs/sdk/versions.md).

| Guide | What you can do |
| --- | --- |
| [Models and authentication](docs/sdk/models.md) | Choose a model, supply credentials, and manage shared connections. |
| [Tools](docs/sdk/tools.md) | Call Python functions and select tools for each turn. |
| [MCP servers](docs/sdk/mcp.md) | Connect servers, configure approval, and inspect tool results. |
| [Structured output](docs/sdk/outputs.md) | Return a validated Python value and check proposed completions. |
| [Conversations](docs/sdk/sessions.md) | Send follow-up messages, update a Session, and stop work. |
| [Streaming and events](docs/sdk/streaming.md) | Display live progress and inspect retained event records. |
| [Save and restore state](docs/sdk/state.md) | Choose storage, compact history, and restore checkpoints. |
| [Configure agent behavior](docs/sdk/configuration.md) | Customize context, callbacks, decisions, and programmatic tool calls. |
| [Guardrails](docs/sdk/guardrails.md) | Check incoming work and final output. |
| [Command execution](docs/sdk/execution.md) | Run commands with explicit workspace and network permissions. |
| [Approvals and manual results](docs/sdk/approvals.md) | Ask for a decision or accept a tool result from your application. |
| [Delegate work](docs/sdk/delegation.md) | Start and control native child agents. |
| [Testing](docs/sdk/testing.md) | Test application behavior with scripted responses and evaluate live models. |
| [Usage and tracing](docs/sdk/observability.md) | Read token usage, export telemetry, and diagnose outcomes. |
| [API reference](docs/sdk/reference.md) | Find supported calls and defaults. |

## More examples

Download the [matching source archive](docs/sdk/installation.md#examples-and-upgrades). With your SDK environment active, run `python -m examples.codex.basic` from the archive root. The four scripts below need an [OpenAI API key](docs/sdk/index.md#2-set-your-api-key-and-run-the-script). The example index also includes tests that run without a key.

- [Function tools](examples/codex/basic.py): Call a Python greeting tool.
- [Typed application context](examples/codex/typed_context.py): Return structured output using application context.
- [Conversation and streaming](examples/codex/conversation.py): Stream a conversation and restore saved state.
- [Validated artifacts](examples/codex/validated_artifact.py): Validate an artifact before writing it.
- [All examples](examples/codex/README.md): Find workflow recipes and their setup requirements.

## Existing v0 applications

You can stay on v0. When you choose to upgrade, we recommend [migrating directly to native v1](MIGRATION.md), including your tools, execution, and state handling. The guide includes an API mapping, validation steps, and instructions for using a coding agent to help with the port.

If a workflow needs the new package before you can port it, this branch provides `agents.classic` as an intermediate step. It retains the Python runner while the root `agents` API uses Codex. See [classic setup](CLASSIC.md) for imports, dependencies, and compatibility limits.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback.

## Contributing

For local SDK development, source builds, and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).
