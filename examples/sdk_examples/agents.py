"""Basic agent creation and usage examples.

Demonstrates how to create agents with different configurations,
run them, and work with their results.

Setup:
    export OPENAI_API_KEY="your-api-key"
    pip install openai-agents

Usage:
    python examples/sdk_examples/agents.py
"""

import asyncio
from dataclasses import dataclass

from agents import Agent, ModelSettings, RunConfig, RunContextWrapper, Runner


# --- Example 1: Minimal Agent ---


async def example_basic_agent() -> None:
    """Create and run a simple agent with default settings."""
    agent = Agent(
        name="Greeter",
        instructions="You are a friendly greeter. Keep responses under 2 sentences.",
    )

    result = await Runner.run(agent, "Hello! My name is Alice.")
    print(f"[Basic Agent] {result.final_output}")


# --- Example 2: Agent with Model Settings ---


async def example_agent_with_model_settings() -> None:
    """Create an agent with custom model settings like temperature."""
    agent = Agent(
        name="Creative Writer",
        instructions="You are a creative writer. Write short, imaginative responses.",
        model_settings=ModelSettings(
            temperature=0.9,
            max_tokens=150,
        ),
    )

    result = await Runner.run(agent, "Describe a sunset on Mars in one sentence.")
    print(f"[Model Settings] {result.final_output}")


# --- Example 3: Agent with Dynamic Instructions ---


@dataclass
class UserContext:
    """Context object carrying user-specific data through the agent run."""

    user_name: str
    language: str


async def example_dynamic_instructions() -> None:
    """Create an agent whose instructions change based on runtime context."""

    def get_instructions(ctx: RunContextWrapper[UserContext], agent: Agent[UserContext]) -> str:
        return (
            f"You are a helpful assistant for {ctx.context.user_name}. "
            f"Always respond in {ctx.context.language}. Keep responses concise."
        )

    agent = Agent[UserContext](
        name="Dynamic Assistant",
        instructions=get_instructions,
    )

    context = UserContext(user_name="Carlos", language="Spanish")
    result = await Runner.run(agent, "What is the capital of France?", context=context)
    print(f"[Dynamic Instructions] {result.final_output}")


# --- Example 4: Agent with RunConfig Override ---


async def example_run_config() -> None:
    """Override model and settings at run time using RunConfig."""
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Be concise.",
    )

    config = RunConfig(
        model="gpt-4.1-mini",
        model_settings=ModelSettings(temperature=0.3),
    )

    result = await Runner.run(agent, "What is 42 * 58?", run_config=config)
    print(f"[RunConfig] {result.final_output}")


# --- Example 5: Multi-turn Conversation ---


async def example_multi_turn() -> None:
    """Continue a conversation across multiple turns using to_input_list()."""
    agent = Agent(
        name="Tutor",
        instructions="You are a math tutor. Give brief explanations.",
    )

    # First turn.
    result = await Runner.run(agent, "What is a prime number?")
    print(f"[Multi-turn] Turn 1: {result.final_output}")

    # Build follow-up input from the previous conversation history.
    follow_up_input = result.to_input_list() + [
        {"role": "user", "content": "Give me 3 examples of prime numbers."}
    ]

    result = await Runner.run(agent, follow_up_input)
    print(f"[Multi-turn] Turn 2: {result.final_output}")


# --- Example 6: Accessing Run Metadata ---


async def example_run_metadata() -> None:
    """Inspect result metadata: usage stats, items generated, and last agent."""
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Be concise.",
    )

    result = await Runner.run(agent, "What is the speed of light?")

    print(f"[Metadata] Output: {result.final_output}")
    print(f"[Metadata] Last agent: {result.last_agent.name}")
    print(f"[Metadata] Items generated: {len(result.new_items)}")
    print(f"[Metadata] Raw responses: {len(result.raw_responses)}")

    # Token usage is tracked on the context wrapper.
    usage = result.context_wrapper.usage
    print(
        f"[Metadata] Usage: {usage.input_tokens} input, "
        f"{usage.output_tokens} output, "
        f"{usage.total_tokens} total tokens"
    )


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Basic Agent Examples")
    print("=" * 60)

    examples = [
        ("1. Basic Agent", example_basic_agent),
        ("2. Model Settings", example_agent_with_model_settings),
        ("3. Dynamic Instructions", example_dynamic_instructions),
        ("4. RunConfig Override", example_run_config),
        ("5. Multi-turn Conversation", example_multi_turn),
        ("6. Run Metadata", example_run_metadata),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
