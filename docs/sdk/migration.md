# Move an application from Agents SDK v0

> **Version:** These guides describe the v1 SDK on `feat/codex-harness`. Build the SDK from the same commit as these guides. See [installation and version compatibility](versions.md) for v0 and v1.

This guide covers migration from Agents SDK v0. If you use the private preview v0.24.0-alpha.5 instead, follow the [alpha.5 migration table](index.md#migrate-from-alpha5).

Port one small workflow into a fresh conversation first. V1 uses the native Codex loop. It does not restore v0 `RunState`, session database rows, handoff state or tracing objects.

## Run a function tool and read a typed result

A v0 application commonly declares an Agent and invokes `Runner.run`. The public baseline for this migration is **v0.24.0**. Its [function-tool export](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/__init__.py), [Runner implementation](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run.py), and [Agent declaration](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py) provide the APIs used below. This fragment illustrates that call shape; it does not establish that your application is compatible with v1. Use a separate v0.24.0 environment to evaluate the v0 workflow:

```text
import asyncio

from pydantic import BaseModel

from agents import Agent, Runner, function_tool


class Answer(BaseModel):
    greeting: str


@function_tool
def greet(name: str) -> str:
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(name="Greeter", tools=[greet], output_type=Answer)
    result = await Runner.run(agent, "Use greet for Ada and return the greeting.")
    print(result.final_output.greeting)
```

The corresponding complete v1 script calls the Agent directly. Install the matching source wheel, set `OPENAI_API_KEY`, save this as `greeting.py`, and run `python greeting.py`:

```python
import asyncio

from pydantic import BaseModel

from agents import Agent, function_tool


class Answer(BaseModel):
    greeting: str


@function_tool
def greet(name: str) -> str:
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(
        name="Greeter",
        instructions="Use greet and return its greeting.",
        tools=[greet],
        output_type=Answer,
    )
    result = await agent.run("Greet Ada.")
    print(result.final_output.greeting)


if __name__ == "__main__":
    asyncio.run(main())
```

The script prints a greeting for Ada; wording can vary. Native structured output has [schema restrictions](configuration.md#structured-output). A final-output validation failure does not undo a completed tool effect. Use the [no-key test](testing.md) to prove the tool result independently of model wording.

## Continue a conversation and supply application state

A v0 application may pass `context=account` to `Runner.run` and supply a v0 session store. In v1, open a Session and inject current context there. This async fragment uses your application-owned `account` and v1 Agent:

```python
async with agent.session(context=account) as session:
    first = await session.submit("Remember that my display label is atlas.")
    await first.get_result()
    second = await session.submit("What label did I give you?")
    result = await second.get_result()
    print(result.final_output)
```

V1 tools read application context through `ToolContext[Account]`; see the complete [context example](tools.md#application-context). Context objects do not become model input automatically. Choose [checkpoint or local storage](sessions.md#choose-how-to-store-a-conversation) explicitly. Rebind current tools, credentials, context and permissions on restore. Importing old database rows or serialized state is unsupported.

## Decide what must stay on v0

| V0 dependency | V1 choice and limit |
| --- | --- |
| `Runner.run` and simple function tools | Use `agent.run` and `from agents import function_tool`; verify arguments and returned values with [ScriptedResponses](testing.md). |
| Handoffs or changing active Agent identity | No automatic handoff migration. [Native children](tools.md#native-children) share the Session's declared authority and return text; they do not implement v0 handoff semantics. Stay on v0 if that contract is required. |
| Input/output guardrails and tripwires | No drop-in v0 guardrail interface. Validate application input before submission and authorize effects inside trusted tool code. Tool policies and final output validation act at different boundaries. Stay on v0 when v0 guardrail ordering or tripwire behavior is required. |
| V0 model-provider adapters or Chat Completions endpoints | V1 accepts `NativeProvider` with a Responses connection. Run the [feature-specific checks](providers.md); a successful text request proves only text. Keep v0 or your own Responses loop when its adapter/loop is required. |
| V0 sessions, `RunState`, realtime or trace integrations | No automatic conversion. Start a fresh v1 conversation and inspect current [history](sessions.md#compact-and-inspect-history), [telemetry](configuration.md#telemetry) and protocol adapters separately. |
| A hard token, cost or wall-clock budget | Usage is observation, not a hard spending ceiling. Application deadlines and cancellation join cleanup but cannot undo effects or stop a synchronous Python thread immediately. Keep an existing proven enforcement boundary when a strict ceiling is required. |

A tool approval controls the declared tool call; it does not authorize all later application effects. Keep caller identity, effect deduplication and permission checks in current application code. Read [feature combinations](choosing.md#choose-feature-combinations) before combining storage, manual replies, code execution and children.
