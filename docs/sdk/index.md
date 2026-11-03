# Build agents with Codex

> **Version:** These guides describe the v1 SDK on `feat/codex-harness`. Build the SDK from the same commit as these guides. See [installation and version compatibility](versions.md) for v0 and v1.

The Agents SDK runs agents from Python using Codex. Give an Agent instructions and tools, then call it to get a result.

See [Choose how to run your agent](choosing.md) for deployment choices and guidance on existing integrations.

Start with an Agent and `@function_tool`. Use `await agent.run(input)` for one answer. Use `agent.session()` when you need more turns, streamed observations or a saved conversation.

Use the [version selector and machine-readable topic index](versions.md) to keep installation, guides, examples and reference on the same interface. For an existing v0 application, start with the [migration cookbook](migration.md).

## Before you begin

Use the Python environment containing a wheel built from this source revision. The [source-build guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/packages/codex/MAINTAINING.md#build-from-this-source) supplies the build and installation steps. Keep v0 and v1 in separate environments, and use examples from the commit you built.

The live example needs an OpenAI API key and makes model requests. For a deterministic check without a model service or real API key, use the [testing guide](testing.md).

## Run your first agent

Importing `agents` and declaring agents or tools perform no I/O. Save this complete script as `agent.py` in your project directory:

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
.venv/bin/python agent.py
```

On Windows PowerShell, use `$env:OPENAI_API_KEY = "your-key"`, then `.\.venv\Scripts\python.exe agent.py`. In a synchronous application, use `agent.run_sync` outside an existing event loop. The [basic example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/basic.py) shows the same complete pattern.

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

Find worked examples for [images, retrieval, application memory, approvals, tools and specialist Agents](recipes.md). The [feature decision table](choosing.md#choose-feature-combinations) explains combinations that limit save/restore or delegation.

## How the runtime fits

The [core concepts guide](concepts.md#how-the-runtime-fits) shows how your Python application, embedded Codex, model provider and tools fit together. Start there before configuring native children or remote execution.

## Migrate from alpha.5

The historical private preview v0.24.0-alpha.5 predates this public source branch. If you used that preview, replace `Runner`, `Turn.wait()`, `Session.wait()` and `Session.interrupt()` as shown below. This table does not describe migration from public v0; use the [v0 migration guide](migration.md) for that path.

| Private alpha.5 call or pattern | V1 source branch |
| --- | --- |
| `Runner.run(agent, input)` or `Runner.run_sync(agent, input)` | `agent.run(input)` or `agent.run_sync(input)`. These Agent methods already exist in alpha.5. |
| `await turn.wait()` | `await turn.get_result()`. For a root Turn, this also follows its accepted steering continuations. |
| `await session.wait()` | Retain the Turn returned by `session.submit(...)`, then await `turn.get_result()`. |
| `await session.interrupt(scope="root", mode=...)` | `await turn.cancel(mode=...)` for the captured root. |
| `await session.interrupt(scope="all", mode=...)` | `await session.cancel_all(mode=...)`. |
| Assigning Agent declaration fields | Construct a new Agent or use supported [Session configuration](configuration.md) overrides. Declaration fields are now immutable. |

The cancellation methods join the work they stop. A captured root cannot cancel a later independent submission; root cancellation also leaves independent children running. See [interruption](sessions.md#interrupt-work) for graceful stops and child controls. For streaming, use [the Turn observer pattern](sessions.md#stream-observations) alongside `get_result()`: a `TurnFinished` observation describes a native step and does not prove that all of a root's continuations have completed.

## Test without a model service

The [testing guide](testing.md) runs real Python tools and the native Session against scripted model responses. It includes a complete no-key script, expected output and a pytest entry point.

## More reference

See the [API reference](reference.md) for supported calls and the [troubleshooting guide](troubleshooting.md) for common symptoms.
