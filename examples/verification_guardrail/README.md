# Verification guardrail

An OpenAI Agents SDK example demonstrating the **verify before act** pattern: an agent may only claim (and finalize) a delivery after deterministic verification passes, and an output guardrail trips if it claims completion without a verification receipt.

## What this example demonstrates

- A minimal **MCP server** exposes `verify_claim(claimType, evidence)`.
- **Tool-level enforcement**: `finalize_delivery` re-runs the verification core and refuses unless it passes. This is the real gate — guardrails are the visible layer, tool checks are the binding one.
- **Output guardrail**: if the agent's final output claims the delivery is finalized without a `receiptId`, the run trips.
- The scenario is intentionally generic (delivery confirmation) and runs with a **mock evidence service** — no API keys, no web3. Swap in your own verifier by replacing `verification_core.py`.

## Files

| File | Purpose |
| --- | --- |
| `verification_core.py` | Deterministic six-step verification chain (capture → integrity → authenticity → consistency → judgment → anchor) + mock delivery service |
| `verify_claim_mcp_server.py` | FastMCP server exposing `verify_claim` |
| `agent.py` | OpenAI Agents SDK agent: `finalize_delivery` tool + output guardrail + MCP server |
| `demo.py` | Run everything: MCP calls, tool enforcement, guardrail check, and (with a key) the live agent |

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python demo.py
```

Parts 1-3 are deterministic and need no API key. Part 4 runs the live agent and requires a valid `OPENAI_API_KEY`.

To run Part 4 against an OpenAI-compatible provider (e.g. DeepSeek), set `DEEPSEEK_API_KEY` (optionally `OPENAI_BASE_URL` and `OPENAI_MODEL`). When a custom model is used, the demo falls back to plain-text output, since some providers do not support structured outputs.

## How it works

```text
Agent receives a delivery claim + evidence
        │
        ▼
verify_claim (MCP) → verdict + receipt        # discoverable, optional pre-check
        │
        ▼
finalize_delivery (tool) → re-verifies        # binding: refuses without a pass
        │
        ▼
Output guardrail → trips if "finalized" is claimed without a receiptId
```

## Extending

Replace `verification_core.run_delivery_verification` with any verifier (real carrier API, image forensics, on-chain checks, human review webhook). The MCP tool, the tool-level gate, and the guardrail stay the same.
