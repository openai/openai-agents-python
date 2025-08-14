"""A script to test and demonstrate the structured session storage feature."""

import asyncio
import random
import sqlite3

from agents import Agent, Runner, SQLiteSession, function_tool


async def main():
    # Create a tool
    @function_tool
    def get_random_number(max_val: int) -> int:
        """Get a random number between 0 and max_val."""
        return random.randint(0, max_val)

    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely. When using tools, explain what you're doing.",
        tools=[get_random_number],
    )

    # Create a session with structured storage enabled
    db_path = "structured_conversation_demo.db"
    session = SQLiteSession("demo_session", db_path, structured=True)

    print("=== Structured Session Storage Demo ===")
    print("This demo shows structured storage that makes conversations easy to query.\n")

    # First turn
    print("First turn:")
    print("User: Pick a random number between 0 and 100")
    result = await Runner.run(
        agent,
        "Pick a random number between 0 and 100",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Second turn - the agent will remember the previous conversation
    print("Second turn:")
    print("User: What number did you pick for me?")
    result = await Runner.run(
        agent,
        "What number did you pick for me?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Third turn - another tool call
    print("Third turn:")
    print("User: Now pick a number between 0 and 50")
    result = await Runner.run(
        agent,
        "Now pick a number between 0 and 50",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    print("=== Conversation Complete ===")
    print(f"Data stored in: {db_path}")
    print()

    # Now demonstrate the structured storage benefits
    print("=== Structured Storage Analysis ===")
    print("With structured storage, you can easily query the conversation:")
    print()

    conn = sqlite3.connect(db_path)

    # Show all messages
    print("1. All conversation messages:")
    cursor = conn.execute("""
        SELECT role, content FROM agent_conversation_messages
        WHERE session_id = 'demo_session'
        ORDER BY created_at
    """)
    for role, content in cursor.fetchall():
        content_preview = content[:60] + "..." if len(content) > 60 else content
        print(f"   {role}: {content_preview}")
    print()

    # Show all tool calls
    print("2. All tool calls and results:")
    cursor = conn.execute("""
        SELECT tool_name, arguments, output, status
        FROM agent_tool_calls
        WHERE session_id = 'demo_session'
        ORDER BY created_at
    """)
    for tool_name, arguments, output, status in cursor.fetchall():
        print(f"   Tool: {tool_name}")
        print(f"   Args: {arguments}")
        print(f"   Result: {output}")
        print(f"   Status: {status}")
        print()

    # Show message count by role
    print("3. Message count by role:")
    cursor = conn.execute("""
        SELECT role, COUNT(*) as count
        FROM agent_conversation_messages
        WHERE session_id = 'demo_session'
        GROUP BY role
    """)
    for role, count in cursor.fetchall():
        print(f"   {role}: {count} messages")
    print()

    conn.close()
    session.close()

    print("=== Query Examples ===")
    print("You can now run SQL queries like:")
    print("• SELECT * FROM agent_conversation_messages WHERE role = 'user';")
    print("• SELECT tool_name, COUNT(*) FROM agent_tool_calls GROUP BY tool_name;")
    print("• SELECT * FROM agent_tool_calls WHERE status = 'completed';")
    print()
    print("This makes conversation analysis, debugging, and building editing")
    print("tools much easier than parsing JSON blobs!")


if __name__ == "__main__":
    asyncio.run(main())
