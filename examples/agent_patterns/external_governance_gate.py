"""External governance gate example using run hooks.

This pattern is useful when a tool call must be checked by an external policy
service before it executes. The hook fails closed by raising if the policy
service denies the call or cannot be reached.
"""

import asyncio
import json
from typing import Any, cast

from agents import Agent, ModelSettings, RunContextWrapper, RunHooks, Runner, Tool, function_tool
from agents.tool_context import ToolContext


async def external_policy_allows(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Replace this function with a call to your policy engine."""
    if tool_name == "refund_order" and arguments.get("amount_usd", 0) > 100:
        return False
    return True


class GovernanceHooks(RunHooks):
    async def on_tool_start(self, context: RunContextWrapper, _agent: Agent, tool: Tool) -> None:
        tool_context = cast(ToolContext[Any], context)
        arguments = json.loads(tool_context.tool_arguments or "{}")

        allowed = await external_policy_allows(tool.name, arguments)
        if not allowed:
            raise PermissionError(f"External governance denied tool call: {tool.name}")


@function_tool
async def refund_order(order_id: str, amount_usd: float) -> str:
    """Refund an order."""
    return f"Refunded ${amount_usd:.2f} for order {order_id}"


async def main() -> None:
    agent = Agent(
        name="Refund assistant",
        instructions="Use the refund_order tool when the user asks for a refund.",
        model_settings=ModelSettings(tool_choice="refund_order"),
        tools=[refund_order],
    )

    result = await Runner.run(
        agent,
        "Refund order order_123 for $42",
        hooks=GovernanceHooks(),
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
