# Agent-to-Agent (A2A) Protocol

The Agent-to-Agent protocol provides standardized interfaces for describing, sharing, and integrating AI agents across different systems. This protocol enables seamless interoperability between agent frameworks and platforms.

## Agent Cards

Agent cards provide a standardized way to describe and export agent capabilities, making it easier to share, document, and integrate agents across different systems. The [`AgentCardBuilder`][agents.agent_card_builder.AgentCardBuilder] class converts agent configurations into structured `AgentCard` objects that describe skills, capabilities, and metadata.

### Installation

Install the optional `a2a` dependency group:

```bash
pip install "openai-agents[a2a]"
```

### Basic Usage

```python
from agents import Agent, function_tool
from agents.agent_card_builder import AgentCardBuilder

@function_tool
def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Search results for: {query}"

@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to a recipient."""
    return f"Email sent to {to}"

# Create an agent with tools
research_agent = Agent(
    name="Research Assistant",
    instructions="Help users research topics and communicate findings",
    handoff_description="An AI assistant that can research topics and send email summaries",
    tools=[search_web, send_email],
)

# Build the agent card
builder = AgentCardBuilder(
    agent=research_agent,
    url="https://api.example.com/agents/research",
    version="1.0.0",
)

card = await builder.build()
print(f"Agent: {card.name}")
print(f"Description: {card.description}")
print(f"Skills: {[skill.name for skill in card.skills]}")
```

### Advanced Configuration

You can customize the agent card with additional capabilities and metadata:

```python
from a2a.types import AgentCapabilities

# Configure custom capabilities
capabilities = AgentCapabilities(
    input_modes=["text", "audio"],
    output_modes=["text", "image"],
    supports_streaming=True,
)

builder = AgentCardBuilder(
    agent=research_agent,
    url="https://api.example.com/agents/research",
    version="2.1.0",
    capabilities=capabilities,
    default_input_modes=["text/plain", "audio/wav"],
    default_output_modes=["text/plain", "image/png"],
)

card = await builder.build()
```

### Agent Hierarchies and Handoffs

The card builder automatically handles complex agent hierarchies with handoffs, creating comprehensive skill descriptions that include both direct tools and orchestration capabilities:

```python
# Create specialized agents
email_agent = Agent(
    name="Email Specialist",
    instructions="Handle email operations",
    tools=[send_email],
)

web_agent = Agent(
    name="Web Researcher",
    instructions="Perform web research",
    tools=[search_web],
)

# Create orchestrator agent with handoffs
coordinator = Agent(
    name="Research Coordinator",
    instructions="Coordinate research and communication tasks",
    handoff_description="Orchestrates research and email workflows",
    handoffs=[email_agent, web_agent],
)

# Build card for the coordinator
builder = AgentCardBuilder(
    agent=coordinator,
    url="https://api.example.com/agents/coordinator",
    version="1.0.0",
)

card = await builder.build()

# The card will include:
# - Orchestration skills describing coordination capabilities
# - Skills from handoff agents (email and web tools)
# - Proper skill deduplication across the hierarchy
```

### Understanding Generated Skills

The agent card builder creates several types of skills:

1. **Tool Skills**: Direct mappings from agent tools to skills
2. **Handoff Skills**: Skills representing capabilities of handoff agents
3. **Orchestration Skills**: High-level skills describing coordination and workflow capabilities

Each skill includes:
- **ID**: Unique identifier (e.g., `"ResearchAgent-search_web"`)
- **Name**: Human-readable name derived from tool/capability name
- **Description**: Detailed description from tool docstrings or agent descriptions
- **Tags**: Automatically generated tags for categorization

!!! note

    The card builder automatically handles circular dependencies in agent hierarchies and deduplicates skills to prevent redundancy in the final card.

### Working with Agent Cards

Once you have an `AgentCard`, you can use it for various purposes:

#### Serialization and Sharing

```python
import json

# Convert card to dictionary for serialization
card_dict = card.model_dump()

# Serialize to JSON
card_json = json.dumps(card_dict, indent=2)

# Save to file
with open("research_agent_card.json", "w") as f:
    f.write(card_json)
```

#### Capability Discovery

```python
# Check agent capabilities
print(f"Supports streaming: {card.capabilities.supports_streaming}")
print(f"Input modes: {card.capabilities.input_modes}")
print(f"Output modes: {card.capabilities.output_modes}")

# List all skills
for skill in card.skills:
    print(f"- {skill.name}: {skill.description}")
    print(f"  Tags: {skill.tags}")
```

## AgentCardBuilder API Reference

The [`AgentCardBuilder`][agents.agent_card_builder.AgentCardBuilder] class provides fine-grained control over card generation:

### Core Methods

- `build_tool_skills(agent)`: Extract skills from agent tools
- `build_handoff_skills(agent)`: Extract skills from handoff capabilities
- `build_orchestration_skill(agent)`: Generate orchestration skills for coordination
- `build_agent_skills(agent)`: Build comprehensive skills for a single agent
- `build_skills()`: Build all skills including transitive handoff skills
- `build()`: Generate the complete `AgentCard`

### Customization Options

- `capabilities`: Configure agent capabilities (streaming, input/output modes)
- `default_input_modes`/`default_output_modes`: Set default communication modes
- `url`: Specify where the agent can be accessed
- `version`: Set agent version for tracking and compatibility

## Best Practices

### Card Design

1. **Descriptive Names**: Use clear, descriptive names for agents and tools
2. **Rich Descriptions**: Provide detailed descriptions in tool docstrings and agent configurations
3. **Proper Versioning**: Use semantic versioning for agent cards
4. **Capability Accuracy**: Ensure capabilities accurately reflect agent abilities

### Integration Patterns

1. **A2A Server Integration**: Agent cards are used by A2A servers to describe agent capabilities for discovery
2. **Protocol Compliance**: Cards provide standardized metadata format for A2A protocol compatibility  
3. **Capability Discovery**: Enable automatic discovery of agent skills and supported interaction modes
4. **Version Management**: Support versioning for agent evolution and compatibility tracking

### Performance Considerations

1. **Skill Deduplication**: The builder automatically deduplicates skills across agent hierarchies
2. **Circular Dependency Handling**: Built-in protection against infinite recursion in agent graphs
3. **Async Processing**: All card building operations are async for better performance
4. **Caching**: Consider caching built cards for frequently accessed agents
