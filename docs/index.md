# OpenAI Agents SDK

The [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) enables you to build agentic AI apps in a lightweight, easy-to-use package with very few abstractions. It's a production-ready upgrade of our previous experimentation for agents, [Swarm](https://github.com/openai/swarm/tree/main). The Agents SDK has a very small set of primitives:

-   **Agents**, which are LLMs equipped with instructions and tools
-   **Agents as tools / Handoffs**, which allow agents to delegate to other agents for specific tasks
-   **Guardrails**, which enable validation of agent inputs and outputs

In combination with Python, these primitives are powerful enough to express complex relationships between tools and agents, and allow you to build real-world applications without a steep learning curve. In addition, the SDK comes with built-in **tracing** that lets you visualize and debug your agentic flows, as well as evaluate them and even fine-tune models for your application.

## Why use the Agents SDK

The SDK has two driving design principles:

1. Enough features to be worth using, but few enough primitives to make it quick to learn.
2. Works great out of the box, but you can customize exactly what happens.

Here are the main features of the SDK:

-   **Agent loop**: A built-in agent loop that handles tool invocation, sends results back to the LLM, and continues until the task is complete.
-   **Python-first**: Use built-in language features to orchestrate and chain agents, rather than needing to learn new abstractions.
-   **Agents as tools / Handoffs**: A powerful mechanism for coordinating and delegating work across multiple agents.
-   **Sandbox agents**: Run specialists inside real isolated workspaces with manifest-defined files, sandbox client choice, and resumable sandbox sessions.
-   **Guardrails**: Run input validation and safety checks in parallel with agent execution, and fail fast when checks do not pass.
-   **Function tools**: Turn any Python function into a tool with automatic schema generation and Pydantic-powered validation.
-   **MCP server tool calling**: Built-in MCP server tool integration that works the same way as function tools.
-   **Sessions**: A persistent memory layer for maintaining working context within an agent loop.
-   **Human in the loop**: Built-in mechanisms for involving humans across agent runs.
-   **Tracing**: Built-in tracing for visualizing, debugging, and monitoring workflows, with support for the OpenAI suite of evaluation, fine-tuning, and distillation tools.
-   **Realtime Agents**: Build powerful voice agents with `gpt-realtime-2`, automatic interruption detection, context management, guardrails, and more.

## Agents SDK or Responses API?

The SDK uses the Responses API by default for OpenAI models, but it adds a higher-level runtime around model calls.

Use the Responses API directly when:

-   you want to own the loop, tool dispatch, and state handling yourself
-   your workflow is short-lived and mainly about returning the model's response

Use the Agents SDK when:

-   you want the runtime to manage turns, tool execution, guardrails, handoffs, or sessions
-   your agent should produce artifacts or operate across multiple coordinated steps
-   you need a real workspace or resumable execution through [Sandbox agents](sandbox_agents.md)

You do not need to choose one globally. Many applications use the SDK for managed workflows and call the Responses API directly for lower-level paths.

## Installation

```bash
pip install openai-agents
```

## Hello world example

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_If running this, ensure you set the `OPENAI_API_KEY` environment variable_)

```bash
export OPENAI_API_KEY=sk-...
```

## Start here

-   Build your first text-based agent with the [Quickstart](quickstart.md).
-   Then decide how you want to carry state across turns in [Running agents](running_agents.md#choose-a-memory-strategy).
-   If the task depends on real files, repos, or isolated per-agent workspace state, read the [Sandbox agents quickstart](sandbox_agents.md).
-   If you are deciding between handoffs and manager-style orchestration, read [Agent orchestration](multi_agent.md).

## Choose your path

Use this table when you know the job you want to do, but not which page explains it.

| Goal | Start here |
| --- | --- |
| Build the first text agent and see one complete run | [Quickstart](quickstart.md) |
| Add function tools, hosted tools, or agents as tools | [Tools](tools.md) |
| Run a coding, review, or document agent inside a real isolated workspace | [Sandbox agents quickstart](sandbox_agents.md) and [Sandbox clients](sandbox/clients.md) |
| Decide between handoffs and manager-style orchestration | [Agent orchestration](multi_agent.md) |
| Keep memory across turns | [Running agents](running_agents.md#choose-a-memory-strategy) and [Sessions](sessions/index.md) |
| Use OpenAI models, websocket transport, or non-OpenAI providers | [Models](models/index.md) |
| Review outputs, run items, interruptions, and resume state | [Results](results.md) |
| Build a low-latency voice agent with `gpt-realtime-2` | [Realtime agents quickstart](realtime/quickstart.md) and [Realtime transport](realtime/transport.md) |
| Build a speech-to-text / agent / text-to-speech pipeline | [Voice pipeline quickstart](voice/quickstart.md) |
