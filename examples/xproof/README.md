# OpenAI Agents SDK + xProof Integration

Certify every tool call and agent completion from the OpenAI Agents SDK on-chain with 4W metadata (Who, What, When, Why).

This example is self-contained and works with the base `xproof>=0.1.0` package — no additional sub-modules required.

## Installation

```bash
pip install openai-agents xproof
```

## Quick Start — RunHooks

Copy `XProofRunHooks` from this example into your project, or use it as a reference to build your own. Attach it to any `Runner.run()` call — every tool output and agent response is automatically certified on MultiversX.

```python
import asyncio
from agents import Agent, Runner
from xproof import XProofClient

# XProofRunHooks is defined in main.py — copy it into your project
from main import XProofRunHooks

client = XProofClient(api_key="pm_...")
hooks = XProofRunHooks(client=client, agent_name="my-agent")

async def main():
    agent = Agent(name="my-agent", instructions="You are a research assistant.")
    result = await Runner.run(agent, input="Summarize recent AI safety papers", hooks=hooks)

asyncio.run(main())
```

## Quick Start — TracingProcessor

For span-level certification, use `XProofTracingProcessor`. It certifies completed `tool` and `agent` spans automatically.

```python
from agents import Agent, Runner, add_trace_processor
from xproof import XProofClient
from main import XProofTracingProcessor

client = XProofClient(api_key="pm_...")
add_trace_processor(XProofTracingProcessor(client=client, agent_name="my-agent"))

async def main():
    agent = Agent(name="analyst", instructions="You analyze data.")
    result = await Runner.run(agent, input="Analyze Q3 metrics")
    # All tool and agent spans are certified as they complete

asyncio.run(main())
```

## What Gets Certified

| Event | WHO | WHY |
|-------|-----|-----|
| Tool output (`on_tool_end`) | agent name | `tool_execution` |
| Final agent response (`on_agent_end`) | agent name | `agent_completion` |
| Tool span end | agent name | `tool_span` |
| Agent span end | agent name | `agent_span` |

Every certification anchors a SHA-256 hash on MultiversX mainnet. The content itself never leaves your environment.

## Get a Free API Key

```bash
curl -s -X POST https://xproof.app/api/agent/register \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "my-openai-agent"}' | python3 -m json.tool
```

Returns a trial key with 10 free certifications.

## Running the Demo

```bash
pip install -r requirements.txt
python main.py
```

The demo uses mock objects — no real API key or LLM backend needed.

## Links

- [xProof](https://xproof.app) · [API docs](https://xproof.app/docs) · [PyPI](https://pypi.org/project/xproof/) · [GitHub](https://github.com/jasonxkensei/xproof)
