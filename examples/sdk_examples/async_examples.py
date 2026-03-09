"""Async/await pattern examples.

Demonstrates various async patterns for running agents: concurrent execution,
async tools, async guardrails, and integration with asyncio primitives.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/async_examples.py
"""

import asyncio
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from agents import Agent, Runner, function_tool


# --- Example 1: Basic Async Execution ---


async def example_basic_async() -> None:
    """Run an agent using async/await."""
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Respond in one sentence.",
    )

    # Runner.run() is always async.
    result = await Runner.run(agent, "What is the tallest mountain?")
    print(f"[Basic Async] {result.final_output}")


# --- Example 2: Concurrent Agent Runs ---


class FactOutput(BaseModel):
    topic: str = Field(description="The topic")
    fact: str = Field(description="An interesting fact")


async def example_concurrent_runs() -> None:
    """Run multiple agents concurrently with asyncio.gather."""
    agent = Agent(
        name="Fact Generator",
        instructions="Give one interesting fact about the requested topic. Be concise.",
        output_type=FactOutput,
    )

    topics = ["Mars", "octopus", "quantum physics"]

    # Run all three agent calls in parallel.
    results = await asyncio.gather(
        Runner.run(agent, f"Tell me a fact about {topics[0]}"),
        Runner.run(agent, f"Tell me a fact about {topics[1]}"),
        Runner.run(agent, f"Tell me a fact about {topics[2]}"),
    )

    for result in results:
        fact: FactOutput = result.final_output
        print(f"[Concurrent] {fact.topic}: {fact.fact}")


# --- Example 3: Async Tools ---


@function_tool
async def async_web_lookup(query: str) -> str:
    """Simulate an async web lookup."""
    # Simulate network delay.
    await asyncio.sleep(0.1)
    responses = {
        "python release": "Python 3.12 was released in October 2023.",
        "rust release": "Rust 1.75 was released in December 2023.",
    }
    return responses.get(query.lower(), f"No results found for '{query}'.")


@function_tool
async def async_database_query(table: str) -> str:
    """Simulate an async database query."""
    await asyncio.sleep(0.05)
    data = {
        "users": "Found 1,234 users in the database.",
        "orders": "Found 5,678 orders in the database.",
    }
    return data.get(table.lower(), f"Table '{table}' not found.")


async def example_async_tools() -> None:
    """Use async function tools that perform simulated I/O."""
    agent = Agent(
        name="Research Bot",
        instructions=(
            "You help with research. Use the available tools to look up information. "
            "Be concise in your response."
        ),
        tools=[async_web_lookup, async_database_query],
    )

    result = await Runner.run(agent, "Look up the latest Python release.")
    print(f"[Async Tools] {result.final_output}")


# --- Example 4: Timeout with asyncio ---


async def example_timeout() -> None:
    """Apply a timeout to an agent run using asyncio.wait_for."""
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Be concise.",
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, "What is 2 + 2?"),
            timeout=30.0,
        )
        print(f"[Timeout] Completed: {result.final_output}")
    except TimeoutError:
        print("[Timeout] Agent run timed out!")


# --- Example 5: Shared Context Across Concurrent Runs ---


@dataclass
class SharedStats:
    """Thread-safe stats collector using asyncio lock."""

    total_queries: int = 0
    results: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, query: str, result: str) -> None:
        async with self.lock:
            self.total_queries += 1
            self.results.append(f"{query}: {result}")


async def example_shared_context() -> None:
    """Share state across concurrent agent runs using a dataclass with a lock."""
    stats = SharedStats()

    agent = Agent(
        name="Q&A Bot",
        instructions="Answer the question in one short sentence.",
    )

    async def run_and_record(question: str) -> None:
        result = await Runner.run(agent, question)
        await stats.record(question, str(result.final_output))

    questions = [
        "What color is the sky?",
        "How many legs does a spider have?",
        "What is the chemical symbol for water?",
    ]

    await asyncio.gather(*(run_and_record(q) for q in questions))

    print(f"[Shared Context] Total queries: {stats.total_queries}")
    for entry in stats.results:
        print(f"[Shared Context] {entry[:80]}")


# --- Example 6: Sequential Chain with Async ---


async def example_sequential_chain() -> None:
    """Chain multiple agent runs sequentially, passing output forward."""
    researcher = Agent(
        name="Researcher",
        instructions="Research the topic and provide 3 key facts. Be concise.",
    )

    writer = Agent(
        name="Writer",
        instructions=(
            "Take the research facts provided and write a single polished paragraph. "
            "Be concise."
        ),
    )

    # Step 1: Research.
    research_result = await Runner.run(researcher, "Key facts about honey bees")
    print(f"[Chain] Research done: {len(str(research_result.final_output))} chars")

    # Step 2: Write, using research output as input.
    write_result = await Runner.run(
        writer,
        f"Write a paragraph using these facts:\n{research_result.final_output}",
    )
    print(f"[Chain] Final paragraph: {write_result.final_output}")


# --- Example 7: Async Generator Pattern ---


async def example_async_streaming_collect() -> None:
    """Use async iteration to collect streamed chunks into a list."""
    agent = Agent(
        name="List Maker",
        instructions="List 3 benefits of exercise. Number them 1, 2, 3.",
    )

    result = Runner.run_streamed(agent, input="List benefits of exercise.")

    # Collect all run item events.
    items_seen: list[str] = []
    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            items_seen.append(event.item.type)

    print(f"[Async Collect] Event types seen: {items_seen}")
    print(f"[Async Collect] Final output: {result.final_output}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Async/Await Pattern Examples")
    print("=" * 60)

    examples = [
        ("1. Basic Async", example_basic_async),
        ("2. Concurrent Runs", example_concurrent_runs),
        ("3. Async Tools", example_async_tools),
        ("4. Timeout", example_timeout),
        ("5. Shared Context", example_shared_context),
        ("6. Sequential Chain", example_sequential_chain),
        ("7. Async Streaming Collect", example_async_streaming_collect),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
