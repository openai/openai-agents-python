# OpenAI Agents SDK

The OpenAI Agents SDK is a lightweight yet powerful framework for building multi-agent workflows.

<img src="https://cdn.openai.com/API/docs/images/orchestration.png" alt="Image of the Agents Tracing UI" style="max-height: 803px;">

### Core concepts:

1. [**Agents**](docs/agents.md): LLMs configured with instructions, tools, guardrails, and handoffs
2. [**Handoffs**](docs/handoffs.md): Allow agents to transfer control to other agents for specific tasks
3. [**Guardrails**](docs/guardrails.md): Configurable safety checks for input and output validation
4. [**Tracing**](docs/tracing.md): Built-in tracking of agent runs, allowing you to view, debug and optimize your workflows

Explore the [examples](examples) directory to see the SDK in action.

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
5. We process the tool calls (if any) and append the tool responses messsages. Then we go to step 1.

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

The Agents SDK includes built-in tracing, making it easy to track and debug the behavior of your agents. Tracing is extensible by design, supporting custom spans and a wide variety of external destinations, including [Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents), [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk), and [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk). See [Tracing](http://openai.github.io/openai-agents-python/tracing.md) for more details.

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

The OpenAI Agents SDK can be integrated with the Model Context Protocol ([MCP](https://modelcontextprotocol.github.io/)) to seamlessly use tools from MCP servers. This integration allows you to:

1. Use tools from MCP servers directly in your agents
2. Configure MCP servers using standard configuration files
3. Combine local tools with tools from MCP servers

### Setting up MCP Integration

1. Create an `mcp_agent.config.yaml` file in your project directory that defines your MCP servers:

```yaml
mcp:
  servers:
    fetch:
      command: "uvx"
      args: ["mcp-server-fetch"]
    filesystem:
      command: "npx"
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

2. Create a context object and specify which MCP servers to use with your agent:

```python
from agents import Agent, Run, function_tool

# Create a simple context class that will hold the MCP server registry
class AgentContext:
    def __init__(self, mcp_config_path=None, mcp_config=None):
        self.mcp_config_path = mcp_config_path  # Optional specific path to config file
        self.mcp_config = mcp_config # Optional programmatic setting of MCP server settings

# Create an agent that specifies which MCP servers to use
agent = Agent(
    name="MCP Assistant",
    instructions="You are a helpful assistant.",
    tools=[your_local_tool],  # Local tools you define
    mcp_servers=["fetch", "filesystem"],  # MCP servers to use (must be in config)
)

# Run the agent - tools from specified MCP servers will be automatically loaded
result = await Run.run(
    starting_agent=agent,
    input="Print the first paragraph of https://openai.github.io/openai-agents-python/", # uses MCP fetch server
    context=AgentContext(),  # Server registry loads automatically
)
```

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
