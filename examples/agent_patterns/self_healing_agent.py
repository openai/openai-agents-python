"""Self-healing agent pattern.

When a model calls a tool that does not exist, or produces malformed JSON for
a structured output, the SDK raises ``ModelBehaviorError``.  The default
behaviour is to let that exception propagate and crash the run.

This example shows a loop that catches ``ModelBehaviorError``, appends the
error text to the conversation as a correction, and retries — giving the
model a chance to fix its own mistake before giving up.

Crucially, the retry resumes from ``exc.run_data.new_items``, which contains
all the turns the agent completed before the error occurred.  This avoids
re-running prior work (and re-triggering tool side effects) on every heal.

The pattern is useful when:
- You are using a smaller/cheaper model that occasionally calls wrong tools.
- You want resilience without replacing the model or hard-coding fallbacks.
- The task is long-running and restarting from scratch is expensive.

Run with:
    python -m examples.agent_patterns.self_healing_agent
"""

import asyncio
from typing import Union

from agents import Agent, ModelBehaviorError, Runner, TResponseInputItem, function_tool

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

    On each retry the completed turns from ``exc.run_data.new_items`` are
    preserved and a correction note is appended, so the model continues from
    where it left off rather than restarting the full task.

    Args:
        task: The initial user message to send to the agent.

    Returns:
        The final output string from the agent.

    Raises:
        ModelBehaviorError: If the model keeps misbehaving after all retries.
    """
    input: Union[str, list[TResponseInputItem]] = task
    heals_remaining = MAX_SELF_HEALS

    while True:
        try:
            result = await Runner.run(agent, input)
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

            # Build the correction message to append.
            correction: TResponseInputItem = {
                "role": "user",
                "content": (
                    f"Your previous response caused an error: {exc.message}\n"
                    "Please correct your approach and try again using only the tools available."
                ),
            }

            # Resume from the completed turns rather than restarting from scratch.
            # exc.run_data.new_items holds every RunItem produced before the error,
            # so prior tool calls and their results are preserved and not re-run.
            completed_items: list[TResponseInputItem] = (
                [item.to_input_item() for item in exc.run_data.new_items]
                if exc.run_data is not None
                else []
            )

            if completed_items:
                input = completed_items + [correction]
                print(
                    f"[self-heal] Resuming from {len(completed_items)} completed turn(s) "
                    "with correction appended..."
                )
            else:
                # No completed turns to preserve — fall back to the original task.
                input = f"{task}\n\n{correction['content']}"
                print("[self-heal] No prior turns to resume from. Retrying from scratch...")


async def main() -> None:
    task = "What is (7 + 3) multiplied by 4?"

    print(f"Task: {task}")
    print("-" * 60)

    output = await run_with_self_healing(task)

    print("-" * 60)
    print(f"Answer: {output}")


if __name__ == "__main__":
    asyncio.run(main())
