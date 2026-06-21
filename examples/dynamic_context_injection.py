"""
Dynamic context injection with RunContextWrapper.

This example demonstrates how to use ``RunContextWrapper[TContext]`` to pass
typed, per-run context — such as user identity, preferences, and permission
tier — to both tool functions and lifecycle hooks without relying on global
state.

Key concepts shown:

* **``RunContextWrapper[UserContext]``** — a typed wrapper that carries your
  custom context object through every tool call and hook invocation for a
  single ``Runner.run()`` call.  The LLM never sees the
  ``RunContextWrapper`` object itself; the wrapper is exclusively for your
  own code.  **Important:** any values you explicitly copy from context
  into a tool's return value *will* be visible to the model as the tool
  result — keep sensitive fields out of tool outputs if you need them
  hidden from the LLM.

* **Context-aware tools** — ``@function_tool`` functions that accept
  ``RunContextWrapper[UserContext]`` as their first parameter and branch on
  the caller's ``price_tier`` to return personalised responses.

* **``AgentHooks``** — a lifecycle hook class whose ``on_tool_end`` callback
  receives the same ``RunContextWrapper``, allowing per-user audit logging or
  side-effects after each tool invocation.

* **Isolation** — three separate ``Runner.run()`` calls share the same
  ``pricing_agent`` but each carries an independent ``UserContext``, so
  free/pro/enterprise users receive different prices from the same agent and
  tool implementations.

Run with:
    OPENAI_API_KEY=sk-... uv run python examples/dynamic_context_injection.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from agents import Agent, Runner, function_tool, trace
from agents.lifecycle import RunHooks
from agents.run_context import RunContextWrapper
from agents.tool import Tool

# ---------------------------------------------------------------------------
# Context dataclass
# ---------------------------------------------------------------------------


@dataclass
class UserContext:
    """Per-run user context passed to tools and lifecycle hooks.

    Attributes:
        user_id: Unique identifier for the user.
        name: Display name used in greetings.
        preferred_currency: ISO 4217 currency code for price display.
        price_tier: Subscription tier that controls which prices are shown.
    """

    user_id: str
    name: str
    preferred_currency: str
    price_tier: Literal["free", "pro", "enterprise"]


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------

_TIER_PRICES: dict[str, int] = {
    "free": 29,
    "pro": 19,
    "enterprise": 9,
}


# ---------------------------------------------------------------------------
# Context-aware tools
# ---------------------------------------------------------------------------


@function_tool
def get_price(
    context: RunContextWrapper[UserContext],
    product_name: str,
) -> str:
    """Return the price for a product based on the caller's subscription tier.

    The price is calculated from the caller's subscription tier (stored in
    context and never exposed to the LLM directly), so each tier receives
    a different price without the tier label appearing in the tool output.

    Args:
        context: Run context containing the caller's ``UserContext``.
        product_name: The name of the product to price.

    Returns:
        A human-readable price string in the user's preferred currency.
        The subscription tier is intentionally omitted from the output.
    """
    user: UserContext = context.context
    price = _TIER_PRICES.get(user.price_tier, _TIER_PRICES["free"])
    symbol = "$" if user.preferred_currency == "USD" else user.preferred_currency + " "
    return f"{product_name} costs {symbol}{price}/month."


@function_tool
def get_greeting(context: RunContextWrapper[UserContext]) -> str:
    """Return a personalised greeting for the current user.

    Args:
        context: Run context containing the caller's ``UserContext``.

    Returns:
        A greeting string addressed to the user by name.
    """
    user: UserContext = context.context
    return f"Hello {user.name}!"


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class PricingHooks(RunHooks[UserContext]):
    """Lifecycle hooks that log tool completions with per-user context.

    ``AgentHooks`` is attached to a ``Runner.run()`` call via the ``hooks``
    parameter.  Each hook method receives the same ``RunContextWrapper`` that
    was passed to ``Runner.run()``, so it can read ``user_id``, ``price_tier``,
    or any other field without extra arguments.
    """

    async def on_tool_end(
        self,
        context: RunContextWrapper[UserContext],
        agent: Agent[UserContext],
        tool: Tool,
        result: object,
    ) -> None:
        """Log a one-line audit record after every tool invocation.

        Args:
            context: Run context providing the active ``UserContext``.
            agent: The agent that invoked the tool.
            tool: The tool that just finished executing.
            result: The value returned by the tool.
        """
        user: UserContext = context.context
        print(f"[{user.user_id}] Tool '{tool.name}' returned for {user.price_tier} user")


# ---------------------------------------------------------------------------
# Agent definition (module-level, shared across all runs)
# ---------------------------------------------------------------------------

pricing_agent: Agent[UserContext] = Agent(
    name="PricingAgent",
    model="gpt-4o-mini",
    tools=[get_price, get_greeting],
    instructions=(
        "Help users with pricing information. "
        "Always greet them first using get_greeting, "
        "then provide their tier-specific pricing using get_price."
    ),
)


# ---------------------------------------------------------------------------
# Per-user runner helper
# ---------------------------------------------------------------------------


async def run_for_user(user: UserContext, query: str) -> None:
    """Run ``pricing_agent`` for a single user and print the final output.

    The ``user`` object is injected into every tool call and hook via
    ``RunContextWrapper``.  The LLM only sees the tool return values, never
    the raw ``UserContext`` fields.

    Args:
        user: The ``UserContext`` for this run.
        query: The user's natural-language request.
    """
    print(f"\n{'─' * 60}")
    print(f"User : {user.name!r}  (id={user.user_id}, tier={user.price_tier})")
    print(f"Query: {query}")
    print("─" * 60)

    result = await Runner.run(
        pricing_agent,
        query,
        context=user,
        hooks=PricingHooks(),
    )

    print(f"Reply: {result.final_output}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Demonstrate dynamic context injection for three different user tiers.

    The same ``pricing_agent`` and tool implementations are reused for every
    call.  Only the ``UserContext`` changes, which causes ``get_price`` to
    return a different number for each user without any branching in the agent
    instructions or any global state.
    """
    query = "What's the price for the Analytics Pro add-on? Please greet me first."

    users: list[UserContext] = [
        UserContext(
            user_id="u001",
            name="Alice",
            preferred_currency="USD",
            price_tier="free",
        ),
        UserContext(
            user_id="u002",
            name="Bob",
            preferred_currency="USD",
            price_tier="pro",
        ),
        UserContext(
            user_id="u003",
            name="Carol",
            preferred_currency="USD",
            price_tier="enterprise",
        ),
    ]

    with trace("dynamic_context_injection"):
        for user in users:
            await run_for_user(user, query)


if __name__ == "__main__":
    asyncio.run(main())
