# Tracing Setup

The tracing setup module provides functions to configure and initialize tracing for OpenAI Agents.

## Functions

### `set_tracing_disabled(disabled: bool) -> None`

Disables or enables tracing globally.

```python
from agents import set_tracing_disabled

# Disable tracing globally
set_tracing_disabled(True)

# Re-enable tracing
set_tracing_disabled(False)
```

### `set_tracing_export_api_key(api_key: str) -> None`

Sets the API key used for exporting traces to OpenAI's backend.

```python
from agents import set_tracing_export_api_key

# Set a specific API key for tracing
set_tracing_export_api_key("sk-your-openai-api-key")
```

### `add_trace_processor(processor: TraceProcessor) -> None`

Adds an additional trace processor to the existing processors.

```python
from agents import add_trace_processor
from agents.tracing.processors import TraceProcessor

class CustomTraceProcessor(TraceProcessor):
    def process_trace(self, trace):
        # Custom processing logic
        print(f"Processing trace: {trace.trace_id}")

# Add custom processor
add_trace_processor(CustomTraceProcessor())
```

### `set_trace_processors(processors: list[TraceProcessor]) -> None`

Replaces all existing trace processors with the provided list.

```python
from agents import set_trace_processors
from agents.tracing.processors import BatchTraceProcessor, BackendSpanExporter

# Replace with custom processors
custom_processors = [
    BatchTraceProcessor(BackendSpanExporter()),
    CustomTraceProcessor()
]
set_trace_processors(custom_processors)
```

## Environment Variables

### `OPENAI_AGENTS_DISABLE_TRACING`

Set to `1` to disable tracing globally.

```bash
export OPENAI_AGENTS_DISABLE_TRACING=1
```

## Common Setup Patterns

### Basic Setup with Custom API Key

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner

# Set up tracing with custom API key
tracing_api_key = os.environ.get("OPENAI_TRACING_API_KEY")
if tracing_api_key:
    set_tracing_export_api_key(tracing_api_key)

agent = Agent(name="Assistant", instructions="You are helpful")
result = await Runner.run(agent, "Hello world")
```

### Setup with Custom Processor

```python
from agents import add_trace_processor, Agent, Runner
from agents.tracing.processors import TraceProcessor
import json

class LoggingTraceProcessor(TraceProcessor):
    def process_trace(self, trace):
        # Log trace to file
        with open("traces.log", "a") as f:
            f.write(json.dumps({
                "trace_id": trace.trace_id,
                "workflow_name": trace.workflow_name,
                "timestamp": trace.started_at.isoformat()
            }) + "\n")

# Add logging processor
add_trace_processor(LoggingTraceProcessor())

agent = Agent(name="Assistant")
result = await Runner.run(agent, "Test message")
```

### Disable Tracing for Specific Runs

```python
from agents import Agent, Runner, RunConfig

agent = Agent(name="Assistant")

# Disable tracing for this specific run
result = await Runner.run(
    agent, 
    "Hello world",
    run_config=RunConfig(tracing_disabled=True)
)
```

## Troubleshooting

### Common Issues

1. **401 Unauthorized Error**: Make sure you have a valid OpenAI API key set
2. **Tracing Not Working**: Check if `OPENAI_AGENTS_DISABLE_TRACING=1` is set
3. **Custom Processors Not Receiving Data**: Ensure processors are added before running agents

### Debug Mode

Enable verbose logging to debug tracing issues:

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```
