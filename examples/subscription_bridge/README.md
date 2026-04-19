# Subscription bridge

This example shows how to drive `openai-agents-python` through a local OpenAI-compatible bridge that routes requests into the authenticated vendor CLIs instead of raw provider APIs:

- `codex/...` -> `codex exec`
- `claude/...` or `anthropic/...` -> `claude -p`

This is useful when you want local agent loops to land on ChatGPT/Codex or Claude Max CLI-backed usage where supported.

Important limitation: this does not make arbitrary raw OpenAI or Anthropic API calls bill to those app plans. The working path is:

`openai-agents-python` -> local bridge -> `codex` / `claude` CLI

## What is included

- `server.py`
  - exposes `/health`, `/v1/chat/completions`, and `/v1/responses`
  - supports non-streaming text responses and structured tool-call loops
- `demo_agent.py`
  - starts an embedded local bridge or connects to an existing one
  - runs a simple tool-using `Agent` through `OpenAIChatCompletionsModel`

## Quick start

Run the embedded demo with Codex:

```bash
uv run --python 3.11 python examples/subscription_bridge/demo_agent.py --backend codex
```

Run the embedded demo with Claude:

```bash
uv run --python 3.11 python examples/subscription_bridge/demo_agent.py --backend claude
```

Use an already-running bridge instead of starting an embedded one:

```bash
uv run --python 3.11 python examples/subscription_bridge/server.py --backend codex --port 8787
uv run --python 3.11 python examples/subscription_bridge/demo_agent.py --backend codex --base-url http://127.0.0.1:8787
```

Override the model or prompt:

```bash
uv run --python 3.11 python examples/subscription_bridge/demo_agent.py \
  --backend codex \
  --model codex/gpt-5.4 \
  --prompt "What is the weather in Tokyo?"
```

## Expected behavior

The demo agent includes a simple `get_weather(city)` function tool. A working run should:

1. ask the bridge for the next assistant turn
2. emit a tool call
3. execute the local tool
4. call the bridge again
5. return plain final assistant text

Example final outputs seen on ATHAME:

- Codex: `The weather in Tokyo is sunny and 72 F.`
- Claude: `The weather in Tokyo is currently sunny with a temperature of 72°F.`

## Verification

Run the targeted tests:

```bash
uv run --python 3.11 pytest tests/examples/test_subscription_bridge.py tests/examples/test_subscription_bridge_demo_agent.py -q
```

## Current limits

Validated:
- chat-completions-compatible tool loop through the bridge
- Codex-backed local tool loop
- Claude-backed local tool loop

Not yet validated:
- streaming responses
- full Responses API parity beyond the current minimal implementation
- multi-agent handoffs and handoff semantics
