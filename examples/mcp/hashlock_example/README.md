# MCP HashLock OTC Example

Connects to the HashLock OTC remote MCP server
(`https://mcp.hashlock.markets/mcp`) over Streamable HTTP and lets the
agent request OTC crypto quotes via HTLC atomic settlement.

Run it with:

```bash
uv run python examples/mcp/hashlock_example/main.py
```

## Prerequisites

Get a HashLock access token (SIWE wallet signature, 30-day expiry,
no email/password):

1. Visit https://hashlock.markets/mcp/auth
2. Sign the login message with your wallet
3. Copy the bearer token
4. Export it:

```bash
export HASHLOCK_ACCESS_TOKEN="<your-token>"
```

## What it demonstrates

- Passing a bearer token to a remote MCP server via the `headers` param
- Requesting a quote with `create_rfq`
- The agent's instructions keep on-chain HTLC writes off-limits — settlement
  happens out-of-band in the user's browser wallet, not from the agent loop

## Tools exposed

- `create_rfq` / `respond_rfq` — post / answer quote requests
- `get_htlc` / `get_rfq` — query trade state
- `create_htlc` / `withdraw_htlc` / `refund_htlc` — HTLC lifecycle
  (read by the agent; writes require user signature)

More: https://github.com/Hashlock-Tech/hashlock-mcp
