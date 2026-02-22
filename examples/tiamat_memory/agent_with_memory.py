"""
Example demonstrating TIAMAT persistent memory with OpenAI Agents.

This example shows how to use TIAMAT's cloud memory API to give agents
persistent cross-session memory — conversations survive restarts, work
across devices, and support full-text search.

Setup:
    pip install openai-agents httpx

Usage:
    # Set your TIAMAT API key (get one free):
    #   curl -X POST https://memory.tiamat.live/api/keys/register \
    #     -H "Content-Type: application/json" \
    #     -d '{"agent_name": "my-agent", "purpose": "demo"}'

    export TIAMAT_API_KEY="your-key-here"
    python agent_with_memory.py
"""

import asyncio
import os

from agents import Agent, Runner
from tiamat_session import TiamatSession


async def main():
    # Get API key from environment or auto-register
    api_key = os.environ.get("TIAMAT_API_KEY")

    if api_key:
        session = TiamatSession(
            session_id="tiamat_demo_conversation",
            api_key=api_key,
        )
    else:
        print("No TIAMAT_API_KEY set — auto-registering a free key...")
        session = await TiamatSession.create(
            session_id="tiamat_demo_conversation",
            agent_name="openai-agents-demo",
            purpose="Demonstrating persistent memory",
        )
        print("API key registered. Set TIAMAT_API_KEY to reuse it.\n")

    # Verify connectivity
    if not await session.ping():
        print("Cannot reach TIAMAT Memory API at https://memory.tiamat.live")
        print("Check your network connection and try again.")
        return

    print("=== TIAMAT Persistent Memory Example ===")
    print("Connected to TIAMAT Memory API")
    print(f"Session ID: tiamat_demo_conversation")
    print("Memories persist across restarts — run this script twice to see!\n")

    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Reply concisely.",
    )

    # First turn
    print("Turn 1:")
    print("User: My name is Alice and I work at Anthropic.")
    result = await Runner.run(
        agent,
        "My name is Alice and I work at Anthropic.",
        session=session,
    )
    print(f"Assistant: {result.final_output}\n")

    # Second turn — agent remembers context
    print("Turn 2:")
    print("User: What's my name and where do I work?")
    result = await Runner.run(
        agent,
        "What's my name and where do I work?",
        session=session,
    )
    print(f"Assistant: {result.final_output}\n")

    # Show stored memories
    items = await session.get_items()
    print(f"=== {len(items)} items stored in TIAMAT ===")
    print("These persist across restarts — no Redis or database needed!")
    print("Powered by https://memory.tiamat.live\n")

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
