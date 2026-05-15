import asyncio
import json
from dataclasses import dataclass

from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    function_tool,
    tool_input_guardrail,
)


@dataclass
class PaymentPolicy:
    max_amount_usd: float
    allowed_merchants: set[str]


@tool_input_guardrail
def enforce_payment_policy(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Check the exact payment payload before the tool is invoked."""
    policy = data.context.context
    args = json.loads(data.context.tool_arguments or "{}")

    if data.context.tool_name != "charge_card":
        return ToolGuardrailFunctionOutput.reject_content("This tool is not allowed.")

    merchant = args.get("merchant")
    amount = float(args.get("amount_usd", 0))

    if merchant not in policy.allowed_merchants:
        return ToolGuardrailFunctionOutput.reject_content(
            f"Payment blocked: merchant {merchant!r} is not approved."
        )

    if amount > policy.max_amount_usd:
        return ToolGuardrailFunctionOutput.reject_content(
            f"Payment blocked: ${amount:.2f} exceeds the ${policy.max_amount_usd:.2f} limit."
        )

    return ToolGuardrailFunctionOutput.allow(
        {"merchant": merchant, "amount_usd": amount, "agent": data.agent.name}
    )


@function_tool(tool_input_guardrails=[enforce_payment_policy])
def charge_card(customer_id: str, merchant: str, amount_usd: float) -> str:
    """Charge a customer's saved card."""
    return f"Charged ${amount_usd:.2f} to {customer_id} for {merchant}."


agent = Agent(
    name="Payment assistant",
    instructions="Help with payments, but respect the payment policy.",
    tools=[charge_card],
)


async def main() -> None:
    policy = PaymentPolicy(max_amount_usd=100.0, allowed_merchants={"example.com"})

    result = await Runner.run(
        agent,
        "Charge customer cus_123 $25 for example.com.",
        context=policy,
    )
    print(result.final_output)

    blocked = await Runner.run(
        agent,
        "Charge customer cus_123 $250 for unknown.example.",
        context=policy,
    )
    print(blocked.final_output)


if __name__ == "__main__":
    asyncio.run(main())
