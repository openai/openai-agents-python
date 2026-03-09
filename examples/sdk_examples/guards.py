"""Input and output guardrail examples.

Demonstrates how to use guardrails to validate agent inputs and outputs,
including both agent-level and tool-level guardrails.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/guards.py
"""

import asyncio
import json
from typing import Any

from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    function_tool,
    input_guardrail,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)


# --- Example 1: Input Guardrail ---


@input_guardrail
def check_input_length(
    ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
) -> GuardrailFunctionOutput:
    """Block inputs that are too long."""
    text = input if isinstance(input, str) else str(input)
    if len(text) > 1000:
        return GuardrailFunctionOutput(
            tripwire_triggered=True,
            output_info={"reason": "Input exceeds 1000 characters", "length": len(text)},
        )
    return GuardrailFunctionOutput(
        tripwire_triggered=False,
        output_info="Input length OK",
    )


@input_guardrail
def check_forbidden_topics(
    ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
) -> GuardrailFunctionOutput:
    """Block inputs that mention forbidden topics."""
    text = input if isinstance(input, str) else str(input)
    forbidden = ["classified", "top secret", "confidential"]
    for word in forbidden:
        if word in text.lower():
            return GuardrailFunctionOutput(
                tripwire_triggered=True,
                output_info={"reason": f"Forbidden topic detected: '{word}'"},
            )
    return GuardrailFunctionOutput(
        tripwire_triggered=False,
        output_info="No forbidden topics detected",
    )


async def example_input_guardrails() -> None:
    """Use input guardrails to validate user input before the agent runs."""
    agent = Agent(
        name="Guarded Assistant",
        instructions="You are a helpful assistant.",
        input_guardrails=[check_input_length, check_forbidden_topics],
    )

    # Normal input should pass.
    result = await Runner.run(agent, "What is the speed of light?")
    print(f"[Input Guard] Normal: {result.final_output}")

    # Input with forbidden topic should be blocked.
    try:
        await Runner.run(agent, "Tell me about classified military operations.")
    except InputGuardrailTripwireTriggered as e:
        print(f"[Input Guard] Blocked: {e.guardrail_result.output.output_info}")


# --- Example 2: Output Guardrail ---


class SummaryOutput(BaseModel):
    summary: str
    word_count: int


@output_guardrail
def validate_summary_output(
    ctx: RunContextWrapper[Any], agent: Agent[Any], output: Any
) -> GuardrailFunctionOutput:
    """Ensure the summary output has a reasonable word count."""
    if isinstance(output, SummaryOutput) and output.word_count <= 0:
        return GuardrailFunctionOutput(
            tripwire_triggered=True,
            output_info="Summary word count must be positive",
        )
    return GuardrailFunctionOutput(
        tripwire_triggered=False,
        output_info="Output validated successfully",
    )


async def example_output_guardrails() -> None:
    """Use output guardrails to validate the agent's final output."""
    agent = Agent(
        name="Summarizer",
        instructions=(
            "Summarize the user's input. Return a JSON object with 'summary' and 'word_count' fields."
        ),
        output_type=SummaryOutput,
        output_guardrails=[validate_summary_output],
    )

    result = await Runner.run(
        agent,
        "The quick brown fox jumps over the lazy dog. This is a classic pangram.",
    )
    print(f"[Output Guard] Summary: {result.final_output}")


# --- Example 3: Async Guardrail ---


@input_guardrail
async def async_content_check(
    ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
) -> GuardrailFunctionOutput:
    """Simulate an async content moderation check."""
    # Simulate async API call for content moderation.
    await asyncio.sleep(0.05)

    text = input if isinstance(input, str) else str(input)
    blocked_patterns = ["spam", "phishing"]

    for pattern in blocked_patterns:
        if pattern in text.lower():
            return GuardrailFunctionOutput(
                tripwire_triggered=True,
                output_info=f"Content blocked: {pattern} detected",
            )
    return GuardrailFunctionOutput(
        tripwire_triggered=False,
        output_info="Async content check passed",
    )


async def example_async_guardrail() -> None:
    """Use an async guardrail for I/O-bound validation."""
    agent = Agent(
        name="Moderated Assistant",
        instructions="You are a helpful assistant.",
        input_guardrails=[async_content_check],
    )

    result = await Runner.run(agent, "Tell me about solar energy.")
    print(f"[Async Guard] Normal: {result.final_output}")

    try:
        await Runner.run(agent, "This is a spam message with phishing links.")
    except InputGuardrailTripwireTriggered as e:
        print(f"[Async Guard] Blocked: {e.guardrail_result.output.output_info}")


# --- Example 4: Tool-Level Guardrails ---


@function_tool
def execute_query(query: str) -> str:
    """Execute a database query and return results."""
    # Simulated database response.
    return json.dumps({"rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]})


@tool_input_guardrail
def block_dangerous_queries(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block SQL injection attempts in tool inputs."""
    args = json.loads(data.context.tool_arguments) if data.context.tool_arguments else {}
    query = str(args.get("query", "")).lower()

    dangerous_keywords = ["drop", "delete", "truncate", "alter"]
    for keyword in dangerous_keywords:
        if keyword in query:
            return ToolGuardrailFunctionOutput.reject_content(
                message=f"Query blocked: contains dangerous keyword '{keyword}'",
                output_info={"blocked_keyword": keyword},
            )

    return ToolGuardrailFunctionOutput(output_info="Query validated")


@tool_output_guardrail
def redact_sensitive_fields(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Check tool output for sensitive data patterns."""
    output_str = str(data.output)

    # Block if SSN pattern is found.
    import re

    if re.search(r"\d{3}-\d{2}-\d{4}", output_str):
        return ToolGuardrailFunctionOutput.raise_exception(
            output_info="Sensitive data (SSN pattern) detected in output",
        )

    return ToolGuardrailFunctionOutput(output_info="Output clean")


async def example_tool_guardrails() -> None:
    """Use tool-level guardrails to validate inputs and outputs of specific tools."""
    # Attach guardrails to the tool.
    execute_query.tool_input_guardrails = [block_dangerous_queries]
    execute_query.tool_output_guardrails = [redact_sensitive_fields]

    agent = Agent(
        name="DB Agent",
        instructions="You query databases. Use the execute_query tool.",
        tools=[execute_query],
    )

    # Safe query should work.
    result = await Runner.run(agent, "Run a query: SELECT * FROM users")
    print(f"[Tool Guard] Safe query: {result.final_output}")

    # Dangerous query should be rejected by the model (not raise an exception).
    result = await Runner.run(agent, "Run a query: DROP TABLE users")
    print(f"[Tool Guard] Dangerous query result: {result.final_output}")


# --- Example 5: Guardrail with Named Decorator ---


@input_guardrail(name="profanity_filter", run_in_parallel=False)
def profanity_check(
    ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
) -> GuardrailFunctionOutput:
    """Check for profanity. Runs before the agent starts (not in parallel)."""
    text = input if isinstance(input, str) else str(input)
    profane_words = ["badword1", "badword2"]
    for word in profane_words:
        if word in text.lower():
            return GuardrailFunctionOutput(
                tripwire_triggered=True,
                output_info=f"Profanity detected: {word}",
            )
    return GuardrailFunctionOutput(
        tripwire_triggered=False,
        output_info="No profanity detected",
    )


async def example_named_guardrail() -> None:
    """Use a named guardrail that runs before the agent (not in parallel)."""
    agent = Agent(
        name="Clean Assistant",
        instructions="You are a helpful, family-friendly assistant.",
        input_guardrails=[profanity_check],
    )

    result = await Runner.run(agent, "What is the meaning of life?")
    print(f"[Named Guard] {result.final_output}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Guardrail Examples")
    print("=" * 60)

    examples = [
        ("1. Input Guardrails", example_input_guardrails),
        ("2. Output Guardrails", example_output_guardrails),
        ("3. Async Guardrail", example_async_guardrail),
        ("4. Tool-Level Guardrails", example_tool_guardrails),
        ("5. Named Guardrail (Sequential)", example_named_guardrail),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
