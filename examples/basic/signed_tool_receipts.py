"""Demonstrate signed, hash-chained receipts for local tool calls.

This example uses RunHooks.on_tool_start and RunHooks.on_tool_end to create two
receipts for each local tool call:

- a pre-execution authorization receipt that commits to the tool call arguments;
- a post-execution settlement receipt that commits to the tool result.

The receipts are canonical JSON, Ed25519-signed, and linked by hash so the chain
can be verified later without trusting the original runtime logs.

Install the optional signing dependency before running:

    pip install cryptography
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agents import Agent, RunContextWrapper, RunHooks, Runner, Tool, function_tool
from agents.tool_context import ToolContext

Receipt = dict[str, Any]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def receipt_hash(receipt: Receipt) -> str:
    unsigned = {k: v for k, v in receipt.items() if k != "signature"}
    return f"sha256:{sha256_hex(unsigned)}"


class SignedReceiptHooks(RunHooks):
    def __init__(self) -> None:
        self.receipts: list[Receipt] = []
        self.signing_key = Ed25519PrivateKey.generate()
        self.verify_key = self.signing_key.public_key()

    def append_receipt(
        self,
        *,
        phase: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments: Any,
        result: Any | None = None,
    ) -> Receipt:
        previous_hash = receipt_hash(self.receipts[-1]) if self.receipts else None
        payload: Receipt = {
            "receipt_version": "openai-agents-signed-tool-receipt-v0",
            "sequence": len(self.receipts) + 1,
            "phase": phase,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "arguments_digest": f"sha256:{sha256_hex(arguments)}",
            "result_digest": f"sha256:{sha256_hex(result)}" if result is not None else None,
            "previous_receipt_hash": previous_hash,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        signature = self.signing_key.sign(canonical_json(payload)).hex()
        receipt = {
            **payload,
            "signature": {
                "alg": "Ed25519",
                "public_key": self.verify_key.public_bytes(
                    encoding=Encoding.Raw,
                    format=PublicFormat.Raw,
                ).hex(),
                "sig": signature,
            },
        }
        self.receipts.append(receipt)
        return receipt

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        tool_context = cast(ToolContext[Any], context)
        receipt = self.append_receipt(
            phase="pre_execution",
            tool_name=tool_context.tool_name or tool.name,
            tool_call_id=tool_context.tool_call_id,
            arguments=tool_context.tool_arguments,
        )
        print(f"[receipt] pre  #{receipt['sequence']} {tool.name}: {receipt_hash(receipt)}")

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: object,
    ) -> None:
        tool_context = cast(ToolContext[Any], context)
        receipt = self.append_receipt(
            phase="post_execution",
            tool_name=tool_context.tool_name or tool.name,
            tool_call_id=tool_context.tool_call_id,
            arguments=tool_context.tool_arguments,
            result=result,
        )
        print(f"[receipt] post #{receipt['sequence']} {tool.name}: {receipt_hash(receipt)}")

    def verify_receipt_chain(self) -> bool:
        previous_hash: str | None = None
        public_key: Ed25519PublicKey = self.verify_key

        for receipt in self.receipts:
            payload = {k: v for k, v in receipt.items() if k != "signature"}
            signature = receipt["signature"]

            if payload["previous_receipt_hash"] != previous_hash:
                return False

            public_key.verify(bytes.fromhex(signature["sig"]), canonical_json(payload))
            previous_hash = receipt_hash(receipt)

        return True


@function_tool
def get_invoice(invoice_id: str) -> dict[str, Any]:
    """Look up an invoice by ID."""
    return {
        "invoice_id": invoice_id,
        "customer": "Acme Co",
        "amount_usd": 125,
        "status": "paid",
    }


@function_tool
def draft_refund(invoice_id: str, amount_usd: int) -> str:
    """Draft a refund for an invoice."""
    return f"Drafted refund for {invoice_id}: ${amount_usd}"


async def main() -> None:
    hooks = SignedReceiptHooks()
    agent = Agent(
        name="Receipts Agent",
        instructions=(
            "Use the tools to look up the invoice and draft a refund for the full amount."
        ),
        tools=[get_invoice, draft_refund],
    )

    await Runner.run(
        agent,
        input="Look up invoice INV-1001 and draft a refund for the full amount.",
        hooks=hooks,
    )

    print("\n--- Signed receipt chain ---")
    print(json.dumps(hooks.receipts, indent=2))
    print(f"\nReceipt chain valid: {hooks.verify_receipt_chain()}")


if __name__ == "__main__":
    asyncio.run(main())
