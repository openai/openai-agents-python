# Model Context Protocol (MCP) in OpenAI Agents SDK

This directory contains examples demonstrating how to use the Model Context Protocol (MCP) with the OpenAI Agents SDK. The integration allows agents to leverage tools from MCP servers alongside native OpenAI Agent SDK tools.

## What is MCP?

The [Model Context Protocol (MCP)](https://modelcontextprotocol.github.io/) is giving models access to tools, resources and prompts in a standardized way. It defines a standardized way, enabling interoperability between different AI systems and tool providers.

## Using MCP servers in Agents SDK

### `mcp_servers` property on Agent

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

### MCP Configuration File

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

## Methods for Configuring MCP

The OpenAI Agents SDK supports several ways to configure MCP servers:

### 1. Automatic Discovery (Recommended)

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

### 2. Explicit Config Path

You can explicitly specify the path to your config file:

```python
class AgentContext:
    def __init__(self, mcp_config_path=None):
        self.mcp_config_path = mcp_config_path  # Will be used to load the config

context = AgentContext(mcp_config_path="/path/to/mcp_agent.config.yaml")
```

### 3. Programmatic Configuration

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

### 4. Custom Server Registry

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

## Examples

### Basic Hello World

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

See [hello_world.py](basic/hello_world.py) for the complete example.

### Streaming Responses

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

See [hello_world_streamed.py](basic/hello_world_streamed.py) for the complete example.

### Slack Integration

An example showing how to use MCP for Slack integration:

```python
agent = Agent(
    name="Slack Finder",
    instructions="""You are an agent with access to Slack conversations.""",
    mcp_servers=["filesystem", "slack"],  # Include the slack MCP server
)

# Search for messages
result = await Runner.run(
    agent, 
    input="What was the last message in the general channel?",
    context=context,
)
```

See [slack.py](basic/slack.py) for the complete example.

## Tips for Working with MCP

1. **Server Configuration**: Ensure your MCP servers are correctly configured in your `mcp_agent.config.yaml` file.

2. **Context Object**: Always provide a context object when running an agent with MCP servers. This is where the server registry will be stored.

3. **Error Handling**: If you encounter errors related to MCP tool schemas, check that your MCP servers are properly installed and configured.

4. **Streaming vs. Non-Streaming**: For streaming responses, use `Runner.run_streamed()` (without await). For non-streaming, use `await Runner.run()`.

5. **Tool Availability**: The MCP tools will be automatically loaded and made available to the agent based on the `mcp_servers` parameter.

6. **Debugging**: Enable verbose logging with `enable_verbose_stdout_logging()` to see detailed information about MCP tool loading and usage.

## Advanced Features

### Custom Tool Handling

You can customize how MCP tools are handled by creating your own MCPAggregator:

```python
from agents.mcp import initialize_mcp_aggregator, mcp_list_tools

# Create a custom aggregator
aggregator = await initialize_mcp_aggregator(
    run_context,
    name="CustomAgent",
    servers=["fetch", "filesystem"],
)

# Get tools from the aggregator
tools = await mcp_list_tools(aggregator)
```

## Troubleshooting

- **Tool Schema Validation Errors**: The MCP bridge automatically sanitizes tool schemas to be compatible with OpenAI's validation requirements. If you encounter schema validation errors, it may be due to unsupported schema properties.

- **Connection Issues**: If you have problems connecting to MCP servers, check that the servers are properly installed and that your configuration file is correctly formatted.

- **Missing Tools**: If expected tools are not available, confirm that the server is correctly defined in your `mcp_servers` list and that the server is properly configured in your `mcp_agent.config.yaml` file.