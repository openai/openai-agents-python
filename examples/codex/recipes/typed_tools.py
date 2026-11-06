"""Validate nested inputs and serialize typed tool results through the native loop.

Pydantic may coerce input values. Its constraints still apply in Python even
when the native schema converter omits them. Output validation runs after the
callback: it cannot roll back an application effect. Use a transaction or an
idempotent writer when that effect needs stronger guarantees.
"""

import asyncio
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from agents import Agent, ToolContext, function_tool
from agents.testing import ScriptedResponses, text, tool_call


class Line(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class Receipt(BaseModel):
    sku: str
    accepted: int


@dataclass
class Ledger:
    writes: list[Line] = field(default_factory=list)
    invalid_return: bool = False


@function_tool
async def record(context: ToolContext[Ledger], line: Line) -> list[Receipt]:
    """Record a synthetic order line and return its receipt."""
    context.context.writes.append(line)
    if context.context.invalid_return:
        # Deliberately malformed data demonstrates validation after the write.
        return [{"sku": line.sku, "accepted": "invalid"}]  # type: ignore[list-item]
    return [Receipt(sku=line.sku, accepted=line.quantity)]


async def run_example() -> tuple[Ledger, ScriptedResponses]:
    ledger = Ledger()
    async with ScriptedResponses(
        [tool_call("record", {"line": {"sku": "synthetic", "quantity": "2"}}, call_id="line")],
        [text("Recorded.")],
    ) as peer:
        await Agent(
            instructions="Use the declared tools.", tools=[record], provider=peer.provider
        ).run("Record the line.", context=ledger)
    return ledger, peer


async def main() -> None:
    ledger, peer = await run_example()
    assert ledger.writes == [Line(sku="synthetic", quantity=2)]
    assert peer.tool_output("line") is not None
    print(peer.tool_output("line"))


if __name__ == "__main__":
    asyncio.run(main())
