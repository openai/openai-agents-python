"""Self-healing agent pattern.

When a model calls a tool that does not exist, or produces malformed JSON for
a structured output, the SDK raises ``ModelBehaviorError``.  The default
behaviour is to let that exception propagate and crash the run.

This example shows a loop that catches ``ModelBehaviorError``, appends the
error text to the conversation as a user-turn correction, and retries — giving
the model a chance to fix its own mistake before giving up.

The pattern is useful when:
- You are using a smaller/cheaper model that occasionally calls wrong tools.
- You want resilience without replacing the model or hard-coding fallbacks.
- The task is long-running and restarting from scratch is expensive.

Run with:
    python -m examples.agent_patterns.self_healing_agent
"""

import asyncio

from agents import Agent, ModelBehaviorError, Runner, function_tool

MAX_SELF_HEALS = 3


@function_tool
def add(a: int, b: int) -> int:
    """Return the sum of two integers.

    Args:
        a: First integer.
        b: Second integer.
    """
    return a + b


@function_tool
def multiply(a: int, b: int) -> int:
    """Return the product of two integers.

    Args:
        a: First integer.
        b: Second integer.
    """
    return a * b


agent = Agent(
    name="Math Assistant",
    instructions=(
        "You are a math assistant. Use the available tools to answer questions. "
        "Only call tools that exist: add, multiply."
    ),
    tools=[add, multiply],
)


async def run_with_self_healing(task: str) -> str:
    """Run an agent task, retrying up to MAX_SELF_HEALS times on ModelBehaviorError.

    On each retry the error description is appended to the conversation so the
    model can understand what went wrong and correct itself.

    Args:
        task: The initial user message to send to the agent.

    Returns:
        The final output string from the agent.

    Raises:
        ModelBehaviorError: If the model keeps misbehaving after all retries.
    """
    messages: str | list = task
    heals_remaining = MAX_SELF_HEALS

    while True:
        try:
            result = await Runner.run(agent, messages)
            return result.final_output
        except ModelBehaviorError as exc:
            if heals_remaining <= 0:
                print(f"[self-heal] Giving up after {MAX_SELF_HEALS} attempts.")
                raise

            heals_remaining -= 1
            print(
                f"[self-heal] ModelBehaviorError caught "
                f"({MAX_SELF_HEALS - heals_remaining}/{MAX_SELF_HEALS}): {exc.message}"
            )

            # Feed the error back so the model can self-correct on the next turn.
            correction = (
                f"Your previous response caused an error: {exc.message}\n"
                "Please correct your approach and try again using only the tools available."
            )

            # Build a fresh input that includes the correction.
            # We restart from the original task plus the correction note so the
            # model has full context without the broken tool call in its history.
            messages = f"{task}\n\n[System note: {correction}]"
            print("[self-heal] Retrying with correction appended to input...")


async def main() -> None:
    task = "What is (7 + 3) multiplied by 4?"

    print(f"Task: {task}")
    print("-" * 60)

    output = await run_with_self_healing(task)

    print("-" * 60)
    print(f"Answer: {output}")


if __name__ == "__main__":
    asyncio.run(main())
