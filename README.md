# OpenAI Agents SDK

Build Python agents that call your functions, use MCP tools, and keep a conversation across messages. Define the agent's instructions and tools; the SDK uses Codex to coordinate model requests and tool calls. It validates tool arguments, calls your functions, and parses typed results.

Your application runs the agent and owns its workers, storage, and credentials. See [Choose how to run your agent](docs/sdk/choosing.md) to compare this model with the hosted Agents API.

## Install

The commands below install the published preview. Start with the [alpha.7 quickstart](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/index.md#run-your-first-agent) and the [alpha.7 examples](https://github.com/openai/openai-agents-python/tree/v0.24.0-alpha.7/examples/codex).

Install [0.24.0-alpha.7](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.7) from a prebuilt Python wheel. Use standard CPython 3.10–3.14 and [create a virtual environment](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/installation.md#create-a-python-environment), then run the command for your platform. The wheel includes the native runtime, so you do not need a compiler or Rust toolchain.

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

This preview is distributed through GitHub Releases. The `openai-agents` package currently on PyPI is the previous SDK. See the [alpha.7 installation guide](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/installation.md) for setup, supported platforms, and troubleshooting.

<a id="example"></a>

## Run your first agent

With the SDK installed, save this script as `agent.py` in your project directory. It makes live model requests and can incur charges. For a test that needs no API key, use the [alpha.7 scripted testing guide](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/testing.md).

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

Set an [OpenAI API key](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/index.md#run-your-first-agent), then run `python agent.py` in your activated environment.

The example prints a greeting for Ada, such as `Hello, Ada!`. The wording can vary. The `@function_tool` decorator exposes the function's name and typed arguments to the model. When the model requests `greeting`, the SDK calls your Python function and sends its result back to the model.

## Development guides

The guides below follow this branch and include APIs added since alpha.7. Follow the [source-build instructions](CONTRIBUTING.md#build-the-sdk-from-source) to use them with a matching SDK build. For an installed preview wheel, see [releases and documentation](docs/sdk/versions.md).

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

Download and extract the [alpha.7 source archive](https://github.com/openai/openai-agents-python/archive/refs/tags/v0.24.0-alpha.7.zip). With your SDK environment active, run `python -m examples.codex.basic` from the archive root. The four scripts below need the API key from the [quickstart](#run-your-first-agent). The example index also includes tests that run without a key.

- [Function tools](https://github.com/openai/openai-agents-python/blob/v0.24.0-alpha.7/examples/codex/basic.py) for calling a Python greeting tool.
- [Typed application context](https://github.com/openai/openai-agents-python/blob/v0.24.0-alpha.7/examples/codex/typed_context.py) for returning structured output.
- [Conversation and streaming](https://github.com/openai/openai-agents-python/blob/v0.24.0-alpha.7/examples/codex/conversation.py) for streaming and restoring a Session.
- [Validated artifacts](https://github.com/openai/openai-agents-python/blob/v0.24.0-alpha.7/examples/codex/validated_artifact.py) for checking an artifact before writing it.
- [All alpha.7 examples](https://github.com/openai/openai-agents-python/tree/v0.24.0-alpha.7/examples/codex).

## Existing v0 applications

You can stay on v0. When you choose to upgrade, we recommend [migrating directly to native v1](MIGRATION.md), including your tools, execution, and state handling. The guide includes an API mapping, validation steps, and instructions for using a coding agent to help with the port.

If a workflow needs the new package before you can port it, this branch provides `agents.classic` as an intermediate step. It retains the Python runner while the root `agents` API uses Codex. See [classic setup](CLASSIC.md) for imports, dependencies, and compatibility limits.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback.

## Contributing

For local SDK development, source builds, and tests, see [CONTRIBUTING.md](CONTRIBUTING.md).
