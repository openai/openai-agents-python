# Workflow Examples

This directory contains examples demonstrating the declarative Workflow system for orchestrating multi-agent interactions.

## Overview

The Workflow system provides a clean, declarative way to define complex multi-agent flows using different connection types:

- **HandoffConnection**: Transfers control between agents (conversation takeover)
- **ToolConnection**: Uses agents as tools (functional calls)  
- **SequentialConnection**: Chains agents with data transformation

## Examples

### [basic_workflow.py](./basic_workflow.py)

Demonstrates a complete workflow with all connection types:

```python
workflow = Workflow([
    HandoffConnection(triage_agent, content_agent),
    ToolConnection(content_agent, analysis_agent),
    SequentialConnection(content_agent, summary_agent)
])

result = await workflow.run("Your request here")
```

## Key Features

- **Declarative**: Define workflows as connection sequences
- **Type-safe**: Full TypeScript-style type safety with generics
- **Flexible**: Mix and match different connection patterns
- **Traceable**: Built-in tracing and observability
- **Async/Sync**: Support for both execution modes
- **Validation**: Automatic workflow validation
- **Modular**: Easy to extend with new connection types

## Usage Patterns

1. **Linear Workflows**: Simple A→B→C chains
2. **Branching Logic**: Conditional connections based on context
3. **Tool Integration**: Agents calling other agents as functions
4. **State Management**: Shared context across workflow steps
