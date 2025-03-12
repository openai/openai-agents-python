# OpenAI Agents SDK (MCP Compatible)

!!!
**This is a fork of the OpenAI Agents SDK with MCP compatibility using [mcp-agent](https://github.com/lastmile-ai/mcp-agent)**
!!!


The OpenAI Agents SDK is a lightweight yet powerful framework for building multi-agent workflows.

<img src="https://cdn.openai.com/API/docs/images/orchestration.png" alt="Image of the Agents Tracing UI" style="max-height: 803px;">

### Core concepts:

1. [**Agents**](https://openai.github.io/openai-agents-python/agents): LLMs configured with instructions, tools, guardrails, and handoffs
2. [**Handoffs**](https://openai.github.io/openai-agents-python/handoffs/): Allow agents to transfer control to other agents for specific tasks
3. [**Guardrails**](https://openai.github.io/openai-agents-python/guardrails/): Configurable safety checks for input and output validation
4. [**Tracing**](https://openai.github.io/openai-agents-python/tracing/): Built-in tracking of agent runs, allowing you to view, debug and optimize your workflows
5. [**MCP**](#using-with-mcp-model-context-protocol): Supports using MCP servers with the Agent abstraction

Explore the [examples](examples) directory to see the SDK in action, and read our [documentation](https://openai.github.io/openai-agents-python/) for more details.

Notably, our SDK [is compatible](https://openai.github.io/openai-agents-python/models/) with any model providers that support the OpenAI Chat Completions API format.

## Get started

1. Set up your Python environment

```
python -m venv env
source env/bin/activate
```

2. Install Agents SDK

```
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

## Handoffs example

```py
from agents import Agent, Runner
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
)


async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
    # ¡Hola! Estoy bien, gracias por preguntar. ¿Y tú, cómo estás?


if __name__ == "__main__":
    asyncio.run(main())
```

## Functions example

```python
import asyncio

from agents import Agent, Runner, function_tool


@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny."


agent = Agent(
    name="Hello world",
    instructions="You are a helpful agent.",
    tools=[get_weather],
)


async def main():
    result = await Runner.run(agent, input="What's the weather in Tokyo?")
    print(result.final_output)
    # The weather in Tokyo is sunny.


if __name__ == "__main__":
    asyncio.run(main())
```

## The agent loop

When you call `Runner.run()`, we run a loop until we get a final output.

1. We call the LLM, using the model and settings on the agent, and the message history.
2. The LLM returns a response, which may include tool calls.
3. If the response has a final output (see below for the more on this), we return it and end the loop.
4. If the response has a handoff, we set the agent to the new agent and go back to step 1.
5. We process the tool calls (if any) and append the tool responses messages. Then we go to step 1.

There is a `max_turns` parameter that you can use to limit the number of times the loop executes.

### Final output

Final output is the last thing the agent produces in the loop.

1.  If you set an `output_type` on the agent, the final output is when the LLM returns something of that type. We use [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) for this.
2.  If there's no `output_type` (i.e. plain text responses), then the first LLM response without any tool calls or handoffs is considered as the final output.

As a result, the mental model for the agent loop is:

1. If the current agent has an `output_type`, the loop runs until the agent produces structured output matching that type.
2. If the current agent does not have an `output_type`, the loop runs until the current agent produces a message without any tool calls/handoffs.

## Common agent patterns

The Agents SDK is designed to be highly flexible, allowing you to model a wide range of LLM workflows including deterministic flows, iterative loops, and more. See examples in [`examples/agent_patterns`](examples/agent_patterns).

## Tracing

The Agents SDK automatically traces your agent runs, making it easy to track and debug the behavior of your agents. Tracing is extensible by design, supporting custom spans and a wide variety of external destinations, including [Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents), [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk), and [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk). For more details about how to customize or disable tracing, see [Tracing](http://openai.github.io/openai-agents-python/tracing).

## Development (only needed if you need to edit the SDK/examples)

0. Ensure you have [`uv`](https://docs.astral.sh/uv/) installed.

```bash
uv --version
```

1. Install dependencies

```bash
make sync
```

2. (After making changes) lint/test

```
make tests  # run tests
make mypy   # run typechecker
make lint   # run linter
```

## Using with MCP (Model Context Protocol)

The OpenAI Agents SDK can be integrated with the Model Context Protocol ([MCP](https://modelcontextprotocol.github.io/)) to seamlessly use tools from MCP servers. The integration allows agents to leverage tools from MCP servers alongside native OpenAI Agent SDK tools:

1. Use tools from MCP servers directly in your agents
2. Configure MCP servers using standard configuration files
3. Combine local tools with tools from MCP servers

### Using MCP servers in Agents SDK

#### `mcp_servers` property on Agent

You can specify the names of MCP servers to give an Agent access to by
setting its `mcp_servers` property.

The Agent will then automatically aggregate tools from the servers, as well as 
any `tools` specified, and create a single extended list of tools. This means you can seamlessly 
use local tools, MCP servers, and other kinds of Agent SDK tools through a single unified syntax.

```python

agent = Agent(
    name="MCP Assistant",
    instructions="You are a helpful assistant with access to MCP tools.",
    tools=[your_other_tools], # Regular tool use for Agent SDK
    mcp_servers=["fetch", "filesystem"]  # Names of MCP servers from your config file (see below)
)
```

#### MCP Configuration File

Configure MCP servers by creating an `mcp_agent.config.yaml` file. You can place this file in your project directory or any parent directory. 

Here's an example configuration file that defines three MCP servers:

```yaml
$schema: "https://raw.githubusercontent.com/lastmile-ai/mcp-agent/main/schema/mcp-agent.config.schema.json"

mcp:
  servers:
    fetch:
      command: "uvx"
      args: ["mcp-server-fetch"]
    filesystem:
      command: "npx"
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
    slack:
      command: "npx"
      args: ["-y", "@modelcontextprotocol/server-slack"]
```

For servers that require sensitive information like API keys, you can:
1. Define them directly in the config file (not recommended for production)
2. Use a separate `mcp_agent.secrets.yaml` file (more secure)
3. Set them as environment variables

### Methods for Configuring MCP

The OpenAI Agents SDK supports several ways to configure MCP servers:

#### 1. Automatic Discovery (Recommended)

The simplest approach lets the SDK automatically find your configuration file if it's named `mcp_agent.config.yaml` and `mcp_agent.secrets.yaml`:

```python
from agents import Agent, Runner

# Create an agent that references MCP servers
agent = Agent(
    name="MCP Assistant",
    instructions="You are a helpful assistant with access to MCP tools.",
    mcp_servers=["fetch", "filesystem"]  # Names of servers from your config file
)

# The context object will be automatically populated
class AgentContext:
    pass

result = await Runner.run(agent, input="Hello world", context=AgentContext())
```

#### 2. Explicit Config Path

You can explicitly specify the path to your config file:

```python
class AgentContext:
    def __init__(self, mcp_config_path=None):
        self.mcp_config_path = mcp_config_path  # Will be used to load the config

context = AgentContext(mcp_config_path="/path/to/mcp_agent.config.yaml")
```

#### 3. Programmatic Configuration

You can programmatically define your MCP settings:

```python
from mcp_agent.config import MCPSettings, MCPServerSettings

# Define MCP config programmatically
mcp_config = MCPSettings(
    servers={
        "fetch": MCPServerSettings(
            command="uvx",
            args=["mcp-server-fetch"]
        ),
        "filesystem": MCPServerSettings(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "."]
        )
    }
)

class AgentContext:
    def __init__(self, mcp_config=None):
        self.mcp_config = mcp_config

context = AgentContext(mcp_config=mcp_config)
```

#### 4. Custom Server Registry

You can create and configure your own MCP server registry:

```python
from mcp_agent.mcp_server_registry import ServerRegistry
from mcp_agent.config import get_settings

# Create a custom server registry
settings = get_settings("/path/to/config.yaml")
server_registry = ServerRegistry(config=settings)

# Create an agent with this registry
agent = Agent(
    name="Custom Registry Agent",
    instructions="You have access to custom MCP servers.",
    mcp_servers=["fetch", "filesystem"],
    mcp_server_registry=server_registry  # Use custom registry
)
```

### Examples

#### Basic Hello World

A simple example demonstrating how to create an agent that uses MCP tools:

```python
# Create an agent with MCP servers
agent = Agent(
    name="MCP Assistant",
    instructions="You are a helpful assistant with access to tools.",
    tools=[get_current_weather],  # Local tools
    mcp_servers=["fetch", "filesystem"],  # MCP servers
)

# Run the agent
result = await Runner.run(
    agent,
    input="What's the weather in Miami? Also, can you fetch the OpenAI website?",
    context=AgentContext(),
)

print(result.response.value)
```

See [hello_world.py](examples/mcp/basic/hello_world.py) for the complete example.

#### Streaming Responses

To stream responses instead of waiting for the complete result:

```python
result = Runner.run_streamed(  # Note: No await here
    agent,
    input="Print the first paragraph of https://openai.github.io/openai-agents-python/",
    context=context,
)

# Stream the events
async for event in result.stream_events():
    if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
        print(event.data.delta, end="", flush=True)
```

See [hello_world_streamed.py](examples/mcp/basic/hello_world_streamed.py) for the complete example.

For more details, read the [MCP examples README](examples/mcp/README.md) and try out the [examples/mcp/basic/hello_world.py](examples/mcp/basic/hello_world.py) for a complete working example.

## Acknowledgements

We'd like to acknowledge the excellent work of the open-source community, especially:

-   [Pydantic](https://docs.pydantic.dev/latest/) (data validation) and [PydanticAI](https://ai.pydantic.dev/) (advanced agent framework)
-   [MkDocs](https://github.com/squidfunk/mkdocs-material)
-   [Griffe](https://github.com/mkdocstrings/griffe)
-   [uv](https://github.com/astral-sh/uv) and [ruff](https://github.com/astral-sh/ruff)
-   [MCP](https://modelcontextprotocol.io/introduction) (Model Context Protocol)
-   [mcp-agent](https://github.com/lastmile-ai/mcp-agent)

We're committed to continuing to build the Agents SDK as an open source framework so others in the community can expand on our approach.
