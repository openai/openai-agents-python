# Agent Wallet Example

Demonstrates the **agent wallet pattern**: an agent that can make paid API calls, but only after proving it has the right authorization from its operator.

## The Pattern

When AI agents make financial API calls (paying for data, compute, or services), three questions arise:

1. **Which agent** is making this call?
2. **Does it have permission** to spend money?
3. **Who authorized it**, and when does that authorization expire?

This example shows one way to answer all three before the agent spends anything.

## How It Works

```
Operator (human)
    |
    |-- issues credential --> Agent
    |                          |
    |                          |-- prove identity --> Verifier
    |                          |                        |
    |                          |<-- authorized ---------|
    |                          |
    |                          |-- pay + call ---------> Paid API
```

1. The operator issues a scoped, time-limited credential to the agent
2. Before calling a paid API, the agent proves it holds a valid credential
3. The verifier checks identity, permissions, and expiry
4. Only authorized agents can proceed to the paid endpoint

## Run It

```bash
uv run python examples/agent_wallet/main.py
```

The example runs against a mock paid API server (no real payments). It demonstrates:
- An authorized agent that successfully pays for data
- An unauthorized agent that gets blocked before spending

## Files

- `main.py` -- OpenAI agent with wallet authorization
- `wallet.py` -- Pluggable identity verifier and wallet abstractions
- `mock_server.py` -- Mock paid API server for self-contained demo

## Extending

The `IdentityVerifier` interface in `wallet.py` is pluggable. The example uses a structural verifier (checks credential format). For production, implement the interface with your identity system of choice (DID, JWT, ZKP, etc.).
