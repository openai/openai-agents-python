# MnemoPay Example -- Agent Memory + Wallet

This example demonstrates how to give an OpenAI agent **persistent memory** and
**micropayment** capabilities using [MnemoPay](https://github.com/mnemopay/mnemopay-sdk).

MnemoPay provides 13 tools that connect to an MCP server, giving agents the
ability to remember facts across sessions, charge for work delivered, and
maintain a verifiable reputation.

## Prerequisites

1. Install the MnemoPay integration:

   ```bash
   pip install mnemopay-openai-agents
   ```

2. Set the MnemoPay server URL (or use stdio transport with `npx`):

   ```bash
   export MNEMOPAY_SERVER_URL="http://localhost:3100"
   ```

3. Set your OpenAI API key:

   ```bash
   export OPENAI_API_KEY="sk-..."
   ```

## Running

```bash
python -m examples.mnemopay.agent_memory_wallet
```

## What it does

1. **Memory**: The agent stores and recalls user preferences across conversation
   turns, demonstrating persistent memory that survives session restarts.
2. **Payments**: After delivering a research summary, the agent creates an escrow
   charge and settles it, showing the full payment lifecycle.
3. **Observability**: The agent checks its own wallet balance and reputation score.

## Tools provided by MnemoPay

| Category       | Tools                                        |
| -------------- | -------------------------------------------- |
| Memory         | `remember`, `recall`, `forget`, `reinforce`, `consolidate` |
| Payments       | `charge`, `settle`, `refund`                 |
| Observability  | `balance`, `profile`, `reputation`, `logs`, `history`      |
