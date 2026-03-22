# OpenAI Agents SDK + xProof Integration

Certify every tool call and agent completion from the OpenAI Agents SDK on-chain with 4W metadata.

## Installation

```bash
pip install xproof[openai-agents]
# or: pip install xproof openai-agents
```

## Quick Start — RunHooks

Attach `XProofRunHooks` to any `Runner.run()` call. Every tool output and agent response is automatically certified on MultiversX.

```python
from agents import Agent, Runner
from xproof.integrations.openai_agents import XProofRunHooks

hooks = XProofRunHooks(api_key="pm_...")

agent = Agent(name="research-assistant", instructions="You are a research assistant.")
result = await Runner.run(agent, input="Summarize recent AI safety papers", hooks=hooks)
# Tool calls and final agent response are certified on-chain
```

## Quick Start — TracingProcessor

For span-level certification, use `XProofTracingProcessor`. It certifies completed `tool` and `agent` spans automatically.

```python
from agents import Agent, Runner, add_trace_processor
from xproof.integrations.openai_agents import XProofTracingProcessor

processor = XProofTracingProcessor(api_key="pm_...")
add_trace_processor(processor)

agent = Agent(name="analyst", instructions="You analyze data.")
result = await Runner.run(agent, input="Analyze Q3 metrics")
# All tool and agent spans are certified as they complete
```

## Batch Mode

```python
hooks = XProofRunHooks(
    api_key="pm_...",
    batch_mode=True,  # buffer certs, flush manually or on agent_end
)
# Certifications are batched and sent at once when the agent completes
```

## What Gets Certified

| Event | 4W Metadata |
|-------|-------------|
| `on_tool_end` | WHO: agent name, WHAT: tool output hash, WHY: `tool_execution` |
| `on_agent_end` | WHO: agent name, WHAT: final output hash, WHY: `agent_completion` |
| Tool span end | WHO: agent name, WHAT: span output hash, WHY: `tool_span` |
| Agent span end | WHO: agent name, WHAT: span output hash, WHY: `agent_span` |

## Running the Demo

```bash
pip install -r requirements.txt
python main.py
```

The demo uses mock objects — no API key or LLM backend needed.
