"""
Example demonstrating Shodh-Memory session for persistent cognitive memory.

This example shows how to use Shodh-Memory as a session backend that provides
persistent memory with semantic search, Hebbian learning, and knowledge graphs —
not just conversation history storage, but cognitive memory that strengthens
with use and decays naturally over time.

Prerequisites:
    1. Install shodh-memory: pip install shodh-memory[openai-agents]
       Or download the binary from: https://github.com/varun29ankuS/shodh-memory/releases
    2. Start the shodh-memory server: shodh-memory serve
       (runs on http://localhost:3030 by default)
    3. Set your API key: export SHODH_API_KEY="your-api-key"

Note: This example requires the shodh-memory server to be running externally.
"""

import asyncio
import os
import uuid

from agents import Agent, Runner

try:
    from shodh_memory.integrations.openai_agents import ShodhSession, ShodhTools
except ImportError:
    raise ImportError(
        "shodh-memory is required for this example. "
        "Install with: pip install shodh-memory[openai-agents]"
    )


SHODH_API_KEY = os.environ.get("SHODH_API_KEY", "test-key")
SHODH_SERVER = os.environ.get("SHODH_SERVER_URL", "http://localhost:3030")

# Unique run ID to isolate state per execution — avoids stale results on reruns.
RUN_ID = uuid.uuid4().hex[:8]


async def session_example():
    """Demonstrate persistent conversation memory across agent runs."""

    print("=== Shodh-Memory Session Example ===")
    print(f"Server: {SHODH_SERVER}")
    print()

    # Create a session backed by shodh-memory.
    # Unlike SQLite or Redis sessions that store raw conversation turns,
    # shodh-memory applies biological memory dynamics: memories strengthen
    # with repeated access (Hebbian learning) and decay naturally over time.
    session = ShodhSession(
        session_id=f"shodh_demo_{RUN_ID}",
        server_url=SHODH_SERVER,
        user_id=f"demo-agent-{RUN_ID}",
        api_key=SHODH_API_KEY,
    )

    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Clear any existing session data for a clean demonstration.
    await session.clear_session()
    print("Session cleared for clean demonstration.")
    print("The agent will remember previous messages automatically.\n")

    # First turn.
    print("First turn:")
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Second turn — the agent remembers the previous conversation.
    print("Second turn:")
    print("User: What state is it in?")
    result = await Runner.run(agent, "What state is it in?", session=session)
    print(f"Assistant: {result.final_output}")
    print()

    # Third turn — continuing the conversation.
    print("Third turn:")
    print("User: What's the population of that state?")
    result = await Runner.run(
        agent,
        "What's the population of that state?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    print()

    print("=== Conversation Complete ===")
    print("Notice how the agent remembered the context from previous turns!")
    print()

    # Show stored items.
    print("=== Session Items Demo ===")
    all_items = await session.get_items()
    print(f"Total items in session: {len(all_items)}")

    latest_items = await session.get_items(limit=2)
    print(f"Latest 2 items:")
    for i, msg in enumerate(latest_items, 1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        print(f"  {i}. {role}: {content[:80]}{'...' if len(content) > 80 else ''}")

    # Session isolation — different session_id, separate history.
    print("\n=== Session Isolation Demo ===")
    other_session = ShodhSession(
        session_id=f"shodh_other_{RUN_ID}",
        server_url=SHODH_SERVER,
        user_id=f"demo-agent-{RUN_ID}",
        api_key=SHODH_API_KEY,
    )
    await other_session.clear_session()

    result = await Runner.run(
        agent,
        "Hello, this is a separate conversation!",
        session=other_session,
    )
    print(f"Other session response: {result.final_output}")

    original_count = len(await session.get_items())
    other_count = len(await other_session.get_items())
    print(f"Original session: {original_count} items")
    print(f"Other session: {other_count} items")
    print("Sessions are completely isolated!")

    # Clean up.
    await other_session.clear_session()


async def tools_example():
    """Demonstrate using shodh-memory tools for explicit memory operations."""

    print("\n=== Shodh-Memory Tools Example ===")
    print("Tools let agents explicitly remember, recall, and manage tasks.\n")

    tools = ShodhTools(
        server_url=SHODH_SERVER,
        user_id=f"demo-tools-{RUN_ID}",
        api_key=SHODH_API_KEY,
    )

    agent = Agent(
        name="Memory Agent",
        instructions=(
            "You have persistent memory. Use shodh_remember to store important facts, "
            "shodh_recall to search your memory, and shodh_add_todo for tasks. "
            "Always confirm what you remembered or found."
        ),
        tools=tools.as_list(),
    )

    # Store a preference.
    print("User: Remember that my favorite language is Rust and I work on robotics.")
    result = await Runner.run(
        agent,
        "Remember that my favorite language is Rust and I work on robotics.",
    )
    print(f"Agent: {result.final_output}")
    print()

    # Recall it.
    print("User: What programming language do I prefer?")
    result = await Runner.run(
        agent,
        "What programming language do I prefer?",
    )
    print(f"Agent: {result.final_output}")
    print()

    # Create a task.
    print("User: Add a todo to review the navigation module, high priority.")
    result = await Runner.run(
        agent,
        "Add a todo to review the navigation module, high priority.",
    )
    print(f"Agent: {result.final_output}")
    print()

    print(f"Available tools: {[t.name for t in tools.as_list()]}")
    print("=== Tools Example Complete ===")


async def main():
    await session_example()
    await tools_example()


if __name__ == "__main__":
    asyncio.run(main())
