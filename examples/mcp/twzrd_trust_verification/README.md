# TWZRD Agent Intel - Streamable HTTP Remote MCP Example

This example shows how to use [TWZRD Agent Intel](https://intel.twzrd.xyz) — a zero-install remote MCP server — with the OpenAI Agents SDK to verify the trustworthiness of Solana agent wallets.

## What it does

1. Connects to `https://intel.twzrd.xyz/mcp` via `MCPServerStreamableHttp`
2. Creates an agent with trust verification instructions
3. Calls `score_agent` to get the trust score for a Solana wallet
4. Optionally runs a `preflight_check` before recommending a transaction

## Setup

```bash
pip install openai-agents
export OPENAI_API_KEY=sk-...
```

No TWZRD API key needed — trust scoring is free.

## Run

```bash
python examples/mcp/twzrd_trust_verification/main.py
```

## TWZRD MCP Config

```json
{"mcpServers": {"twzrd-agent-intel": {"url": "https://intel.twzrd.xyz/mcp"}}}
```

## Available Tools

| Tool | Description | Cost |
|------|-------------|------|
| `score_agent(wallet)` | Trust score (0-100) + reputation | Free |
| `resolve_agent(wallet)` | Agent identity resolution | Free |
| `preflight_check(wallet)` | Pre-transaction safety check | Free |
| `verify_trust_receipt(receipt)` | Verify x402 receipt | Free |
| `get_trust_receipt(wallet)` | Full trust receipt | x402 |

PyPI: `pip install twzrd-agent-intel`
