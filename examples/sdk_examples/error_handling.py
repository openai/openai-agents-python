"""Error handling examples.

Demonstrates how to handle various exception types from the Agents SDK,
including guardrail failures, max turns exceeded, model errors, and tool timeouts.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/error_handling.py
"""

import asyncio
from typing import Any

from pydantic import BaseModel

from agents import (
    Agent,
    AgentsException,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    function_tool,
    input_guardrail,
    output_guardrail,
)


# --- Example 1: Catching Guardrail Exceptions ---


@input_guardrail
def block_short_input(
    ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
) -> GuardrailFunctionOutput:
    """Block inputs shorter than 10 characters."""
    text = input if isinstance(input, str) else str(input)
    if len(text) < 10:
        return GuardrailFunctionOutput(
            tripwire_triggered=True,
            output_info={"reason": "Input too short", "min_length": 10, "actual": len(text)},
        )
    return GuardrailFunctionOutput(tripwire_triggered=False, output_info="OK")


async def example_input_guardrail_error() -> None:
    """Catch InputGuardrailTripwireTriggered when input is blocked."""
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
        input_guardrails=[block_short_input],
    )

    try:
        await Runner.run(agent, "Hi")
    except InputGuardrailTripwireTriggered as e:
        print(f"[Input Guard Error] Guardrail: {e.guardrail_result.guardrail.get_name()}")
        print(f"[Input Guard Error] Details: {e.guardrail_result.output.output_info}")
        # Access run data if available.
        if e.run_data:
            print(f"[Input Guard Error] Last agent: {e.run_data.last_agent.name}")


# --- Example 2: Output Guardrail Error ---


class PositiveResponse(BaseModel):
    message: str
    sentiment_score: float


@output_guardrail
def require_positive_sentiment(
    ctx: RunContextWrapper[Any], agent: Agent[Any], output: Any
) -> GuardrailFunctionOutput:
    """Require the output to have a positive sentiment score."""
    if isinstance(output, PositiveResponse) and output.sentiment_score < 0.5:
        return GuardrailFunctionOutput(
            tripwire_triggered=True,
            output_info={
                "reason": "Sentiment too negative",
                "score": output.sentiment_score,
            },
        )
    return GuardrailFunctionOutput(tripwire_triggered=False, output_info="Sentiment OK")


async def example_output_guardrail_error() -> None:
    """Catch OutputGuardrailTripwireTriggered when output fails validation."""
    agent = Agent(
        name="Positive Bot",
        instructions=(
            "Respond to the user with a message and a sentiment_score between 0.0 and 1.0. "
            "Match the sentiment of the input text."
        ),
        output_type=PositiveResponse,
        output_guardrails=[require_positive_sentiment],
    )

    try:
        # Ask something likely to produce a negative sentiment score.
        await Runner.run(agent, "Everything is terrible and I hate everything.")
    except OutputGuardrailTripwireTriggered as e:
        print(f"[Output Guard Error] Guardrail: {e.guardrail_result.guardrail.get_name()}")
        print(f"[Output Guard Error] Agent output: {e.guardrail_result.agent_output}")
        print(f"[Output Guard Error] Details: {e.guardrail_result.output.output_info}")
    except AgentsException:
        # The model may not always produce a negative score; this is demonstration code.
        print("[Output Guard Error] Agent completed without triggering guardrail (model chose high sentiment)")


# --- Example 3: Max Turns Exceeded ---


@function_tool
def think_step(step: str) -> str:
    """Perform a thinking step. Always returns 'continue thinking'."""
    return f"Completed step: {step}. Continue thinking about the next step."


async def example_max_turns() -> None:
    """Catch MaxTurnsExceeded when the agent exceeds the turn limit."""
    agent = Agent(
        name="Thinker",
        instructions=(
            "You are a deep thinker. For every response, use the think_step tool "
            "to reason through each aspect. Always continue thinking."
        ),
        tools=[think_step],
    )

    try:
        # Set a very low max_turns to force the error.
        await Runner.run(agent, "Think about the meaning of life.", max_turns=2)
    except MaxTurnsExceeded as e:
        print(f"[Max Turns] Error: {e.message}")
        if e.run_data:
            print(f"[Max Turns] Items generated: {len(e.run_data.new_items)}")
            print(f"[Max Turns] Last agent: {e.run_data.last_agent.name}")


# --- Example 4: Comprehensive Error Handler ---


async def example_comprehensive_error_handling() -> None:
    """Demonstrate a catch-all pattern for handling all SDK exceptions."""

    @input_guardrail
    def strict_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
    ) -> GuardrailFunctionOutput:
        text = input if isinstance(input, str) else str(input)
        if "blocked" in text.lower():
            return GuardrailFunctionOutput(tripwire_triggered=True, output_info="Blocked word")
        return GuardrailFunctionOutput(tripwire_triggered=False, output_info="OK")

    agent = Agent(
        name="Guarded Bot",
        instructions="You are a helpful assistant.",
        input_guardrails=[strict_check],
    )

    test_inputs = [
        "This message contains a blocked word.",
        "What is the capital of Japan?",
    ]

    for user_input in test_inputs:
        try:
            result = await Runner.run(agent, user_input)
            print(f"[Comprehensive] Success: {result.final_output}")

        except InputGuardrailTripwireTriggered as e:
            print(f"[Comprehensive] Input blocked: {e.guardrail_result.output.output_info}")

        except OutputGuardrailTripwireTriggered as e:
            print(f"[Comprehensive] Output blocked: {e.guardrail_result.output.output_info}")

        except MaxTurnsExceeded as e:
            print(f"[Comprehensive] Too many turns: {e.message}")

        except AgentsException as e:
            # Catch-all for any other SDK exception.
            print(f"[Comprehensive] SDK error: {e}")
            if e.run_data:
                print(f"  Last agent: {e.run_data.last_agent.name}")
                print(f"  Items: {len(e.run_data.new_items)}")


# --- Example 5: Accessing RunErrorDetails ---


async def example_run_error_details() -> None:
    """Access detailed error data from the run when an exception occurs."""
    agent = Agent(
        name="Loopy Agent",
        instructions="Always use the think_step tool. Never stop thinking.",
        tools=[think_step],
    )

    try:
        await Runner.run(agent, "Think hard.", max_turns=2)
    except AgentsException as e:
        if e.run_data:
            print(f"[Error Details] Input: {e.run_data.input}")
            print(f"[Error Details] Last agent: {e.run_data.last_agent.name}")
            print(f"[Error Details] New items: {len(e.run_data.new_items)}")
            print(f"[Error Details] Raw responses: {len(e.run_data.raw_responses)}")

            # Inspect token usage from the context wrapper.
            usage = e.run_data.context_wrapper.usage
            print(
                f"[Error Details] Tokens used: {usage.input_tokens} in, "
                f"{usage.output_tokens} out"
            )
        else:
            print(f"[Error Details] Error (no run data): {e}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Error Handling Examples")
    print("=" * 60)

    examples = [
        ("1. Input Guardrail Error", example_input_guardrail_error),
        ("2. Output Guardrail Error", example_output_guardrail_error),
        ("3. Max Turns Exceeded", example_max_turns),
        ("4. Comprehensive Error Handling", example_comprehensive_error_handling),
        ("5. Accessing RunErrorDetails", example_run_error_details),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
