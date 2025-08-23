# Workflows

!!! warning "Beta Feature"
    
    The Workflow system is currently in beta. The API may change in future releases. We welcome feedback and contributions to help stabilize this feature.

Workflows provide a declarative way to orchestrate complex multi-agent interactions. Instead of manually chaining agent calls in your code, you can define the flow as a sequence of connections between agents, making your multi-agent systems more readable, maintainable, and reusable.

A workflow consists of a sequence of **connections** that define how agents interact with each other. Each connection type implements a different interaction pattern, from simple handoffs to parallel processing.

## Conditional Execution

Workflows now support **conditional execution** - connections may be skipped if their preconditions are not met. For example:

- **SequentialConnection** and **ToolConnection** will only execute if their `from_agent` is currently active
- If a **HandoffConnection** doesn't result in an actual handoff, subsequent connections depending on the target agent will be skipped
- This enables more intelligent workflow routing based on runtime decisions

The workflow execution engine tracks which connections were skipped and reports them in the `WorkflowResult.skipped_connections` field.

## Basic Usage

Here's a simple workflow that chains three agents together:

```python
from agents import Agent, Workflow, HandoffConnection, ToolConnection

# Define your agents
triage_agent = Agent(name="Triage Agent", instructions="Route requests to specialists")
specialist_agent = Agent(name="Specialist", instructions="Handle specialized requests") 
summary_agent = Agent(name="Summary Agent", instructions="Create final summaries")

# Create the workflow
workflow = Workflow([
    HandoffConnection(triage_agent, specialist_agent),
    ToolConnection(specialist_agent, summary_agent),
])

# Execute the workflow
result = await workflow.run("Help me with my request")
print(result.final_result.final_output)
```

## Connection Types

The Workflow system supports several connection types, each implementing different interaction patterns:

### HandoffConnection

Transfers control from one agent to another. The target agent takes over the conversation and sees the full conversation history.

```python
from agents import HandoffConnection

connection = HandoffConnection(
    from_agent=triage_agent,
    to_agent=billing_agent,
    tool_description_override="Transfer to billing specialist",
    input_filter=custom_filter_function,  # Optional
)
```

**Conditional Behavior**: If the `from_agent` chooses not to use the handoff tool, the handoff will not occur, and subsequent connections that depend on the `to_agent` may be skipped.

**Use cases**: Routing, delegation, specialization

### ToolConnection

Uses the target agent as a tool for the source agent. The source agent calls the target agent like a function and continues with the result.

```python
from agents import ToolConnection

connection = ToolConnection(
    from_agent=main_agent,
    to_agent=analysis_agent,
    tool_name="analyze_data",
    tool_description="Get detailed analysis",
    custom_output_extractor=lambda result: result.final_output.summary,
)
```

**Conditional Behavior**: This connection will only execute if the `from_agent` is currently active (either as the last agent or as a handoff target from a previous connection).

**Use cases**: Modular functions, analysis, data processing

### SequentialConnection

Passes the output of one agent as input to another agent, creating a data transformation pipeline.

```python
from agents import SequentialConnection

connection = SequentialConnection(
    from_agent=research_agent,
    to_agent=writer_agent,
    output_transformer=lambda result: f"Research findings: {result.final_output}",
)
```

**Conditional Behavior**: This connection will only execute if the `from_agent` is currently active (either as the last agent or as a handoff target from a previous connection).

**Use cases**: Data pipelines, multi-step transformations, processing chains

### ConditionalConnection

Routes to different agents based on a condition, enabling dynamic workflow branching.

```python
from agents import ConditionalConnection

def should_escalate(context, previous_result):
    return context.context.priority == "high"

connection = ConditionalConnection(
    from_agent=support_agent,
    to_agent=standard_agent,
    alternative_agent=escalation_agent,
    condition=should_escalate,
)
```

**Use cases**: Dynamic routing, conditional logic, adaptive workflows

### ParallelConnection

Runs multiple agents concurrently and optionally synthesizes their results.

```python
from agents import ParallelConnection

connection = ParallelConnection(
    from_agent=coordinator_agent,
    to_agent=coordinator_agent,  # Not used in parallel execution
    parallel_agents=[technical_agent, business_agent, legal_agent],
    synthesizer_agent=synthesis_agent,
    synthesis_template="Combine these perspectives: {results}",
)
```

**Use cases**: Concurrent processing, multiple perspectives, performance optimization

## Workflow Configuration

### Basic Configuration

```python
from agents import Workflow

workflow = Workflow(
    connections=[connection1, connection2, connection3],
    name="My Workflow",                    # Optional: for tracing
    context=my_context,                    # Optional: shared context
    max_steps=50,                          # Optional: safety limit
    trace_workflow=True,                   # Optional: enable tracing
)
```

### Context Management

Workflows support shared context across all agents:

```python
from pydantic import BaseModel

class WorkflowContext(BaseModel):
    user_id: str
    session_data: dict = {}
    
context = WorkflowContext(user_id="123")
workflow = Workflow(connections=[...], context=context)

result = await workflow.run("Hello")
# Context is shared and can be modified by agents
print(result.context.session_data)
```

### Validation

Workflows automatically validate the connection chain:

```python
# Check for validation errors
errors = workflow.validate_chain()
if errors:
    print("Workflow validation failed:")
    for error in errors:
        print(f"  - {error}")
```

## Execution

### Asynchronous Execution

```python
result = await workflow.run("Your input here")

# Access results
print(result.final_result.final_output)  # Final output
print(len(result.step_results))          # Number of steps executed
print(result.skipped_connections)        # Indices of skipped connections
print(result.context)                    # Final context state
```

### Synchronous Execution

```python
# For simpler scripts or testing
result = workflow.run_sync("Your input here")
print(result.final_result.final_output)
```

### Streaming

Workflows support streaming through the underlying Runner:

```python
from agents import Runner

# For streaming, use Runner directly with workflow-prepared agents
prepared_agent = workflow.connections[0].prepare_agent(context_wrapper)
result = Runner.run_streamed(prepared_agent, input_data)

async for event in result.stream_events():
    # Handle streaming events
    pass
```

## Advanced Patterns

### Conditional Execution Example

Create workflows where connections are conditionally executed based on handoff decisions:

```python
workflow = Workflow([
    HandoffConnection(triage_agent, specialist_agent),
    # This will only execute if the handoff above actually occurred
    SequentialConnection(specialist_agent, summary_agent),
])

result = await workflow.run("Simple request")

# If triage_agent handled the request without handoff:
print(result.skipped_connections)  # [1] - second connection was skipped
print(result.final_result.last_agent.name)  # "Triage Agent"

# If triage_agent performed the handoff:
print(result.skipped_connections)  # [] - no connections skipped
print(result.final_result.last_agent.name)  # "Summary Agent"
```

### Dynamic Workflow Construction

Build workflows programmatically based on conditions:

```python
def build_workflow(project_type: str) -> Workflow:
    connections = [
        HandoffConnection(intake_agent, specialist_agent)
    ]
    
    if project_type == "research":
        connections.append(ToolConnection(specialist_agent, research_agent))
    elif project_type == "development":
        connections.append(ToolConnection(specialist_agent, dev_agent))
    
    connections.append(SequentialConnection(specialist_agent, summary_agent))
    
    return Workflow(connections=connections, name=f"{project_type.title()} Workflow")
```

### Workflow Composition

Combine and extend existing workflows:

```python
# Clone and modify workflows
enhanced_workflow = base_workflow.clone(
    name="Enhanced Workflow",
    max_steps=100,
).add_connection(
    SequentialConnection(last_agent, review_agent)
)
```

### Error Handling

Workflows provide structured error handling:

```python
try:
    result = await workflow.run(input_data)
except UserError as e:
    print(f"Workflow configuration error: {e}")
except Exception as e:
    print(f"Workflow execution failed: {e}")
```

## Best Practices

### 1. Design for Clarity

Make your workflows self-documenting:

```python
workflow = Workflow([
    HandoffConnection(
        from_agent=intake_agent,
        to_agent=specialist_agent,
        tool_description_override="Route to appropriate specialist",
    ),
    ToolConnection(
        from_agent=specialist_agent,
        to_agent=analysis_agent,
        tool_name="analyze_request",
        tool_description="Perform detailed analysis",
    ),
], name="Customer Request Processing")
```

### 2. Use Appropriate Connection Types

Choose the right connection type for your use case:

- **HandoffConnection**: When you need full conversation transfer
- **ToolConnection**: When you need modular, reusable functionality
- **SequentialConnection**: When you need data transformation pipelines
- **ConditionalConnection**: When you need dynamic routing
- **ParallelConnection**: When you need concurrent processing

### 3. Leverage Context

Use shared context for coordination:

```python
class ProjectContext(BaseModel):
    requirements: list[str] = []
    stakeholders: list[str] = []
    risk_level: str = "medium"

# Agents can read and modify shared state
@function_tool
def add_requirement(req: str, context: ProjectContext) -> str:
    context.requirements.append(req)
    return f"Added: {req}"
```

### 4. Validate Early

Always validate your workflows before execution:

```python
errors = workflow.validate_chain()
if errors:
    raise ValueError(f"Invalid workflow: {errors}")
```

### 5. Use Tracing

Enable tracing for observability:

```python
workflow = Workflow(
    connections=[...],
    name="My Workflow",
    trace_workflow=True,  # Enables automatic tracing
)
```

## Examples

See the [`examples/workflows`](https://github.com/openai/openai-agents-python/tree/main/examples/workflows) directory for complete examples:

- **[basic_workflow.py](https://github.com/openai/openai-agents-python/tree/main/examples/workflows/basic_workflow.py)**: Simple workflow with all connection types
- **[advanced_workflow.py](https://github.com/openai/openai-agents-python/tree/main/examples/workflows/advanced_workflow.py)**: Complex orchestration patterns
- **[comprehensive_example.py](https://github.com/openai/openai-agents-python/tree/main/examples/workflows/comprehensive_example.py)**: Full-featured workflow system

## Migration from Manual Orchestration

If you're currently orchestrating agents manually, here's how to migrate:

### Before (Manual)

```python
# Manual orchestration
result1 = await Runner.run(agent1, input_data)
result2 = await Runner.run(agent2, result1.final_output) 
result3 = await Runner.run(agent3, result2.to_input_list())
```

### After (Workflow)

```python
# Declarative workflow
workflow = Workflow([
    SequentialConnection(agent1, agent2),
    HandoffConnection(agent2, agent3),
])

result = await workflow.run(input_data)
```

The workflow approach provides better structure, reusability, and built-in features like validation and tracing.
