# Agent-to-Agent (A2A) protocol

The A2A extension lets an [`Agent`][agents.agent.Agent] talk to — and be called by — agents built on *other* frameworks, using the public [A2A protocol](https://a2aproject.github.io/A2A/).

Where the [Model context protocol (MCP)](mcp.md) connects an agent to **tools and resources**, A2A connects an agent to **other agents**. The two are complementary.

The extension ships a spec-aligned subset of the protocol:

- A public **Agent Card** served at `/.well-known/agent-card.json`.
- A **JSON-RPC 2.0** surface for `message/send`, `message/stream` (Server-Sent Events), `tasks/get`, and `tasks/cancel`.

Install the optional dependencies:

```bash
pip install "openai-agents[a2a]"
```

## Publish an agent

[`A2AServer`][agents.extensions.a2a.A2AServer] wraps an agent in a FastAPI app and derives the Agent Card from it:

```python
from agents import Agent
from agents.extensions.a2a import A2AServer

agent = Agent(name="Assistant", instructions="You are a helpful assistant.")

server = A2AServer(agent, url="http://localhost:8000/")
server.run(port=8000)  # requires `uvicorn`
```

`server.app` is a plain FastAPI application, so you can also mount it inside an existing service or serve it with any ASGI server.

## Call a remote agent

[`A2AClient`][agents.extensions.a2a.A2AClient] fetches a peer's card and sends or streams messages:

```python
from agents.extensions.a2a import A2AClient, Task

async with A2AClient("http://localhost:8000/") as client:
    card = await client.get_agent_card()
    print(card.name)

    result = await client.send_message("What is the capital of France?")
    if isinstance(result, Task):
        print(result.status.message)

    async for update in client.stream_message("Tell me a joke."):
        print(update["kind"])
```

## Delegate to a remote agent as a tool

Use [`A2AClient.as_tool`][agents.extensions.a2a.A2AClient.as_tool] to let a local agent delegate to a remote A2A peer, the same way you would with [agents as tools](tools.md):

```python
from agents import Agent, Runner
from agents.extensions.a2a import A2AClient

remote = A2AClient("http://localhost:8000/")
orchestrator = Agent(
    name="Orchestrator",
    instructions="Use the remote agent for geography questions.",
    tools=[remote.as_tool(tool_name="geography_expert")],
)

result = await Runner.run(orchestrator, "What is the capital of France?")
```

## API reference

::: agents.extensions.a2a
