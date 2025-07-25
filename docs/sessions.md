# Sessions

The Agents SDK provides built-in session memory to automatically maintain conversation history across multiple agent runs, eliminating the need to manually handle `.to_input_list()` between turns.

Sessions stores conversation history for a specific session, allowing agents to maintain context without requiring explicit manual memory management. This is particularly useful for building chat applications or multi-turn conversations where you want the agent to remember previous interactions.

## Installation

### SQLite Sessions
SQLite sessions are available by default with no additional dependencies required.

### Redis Sessions
Redis sessions require the Redis package, which is included as a dependency:

```bash
# Redis support is included by default
pip install openai-agents

# Or if you already have the package installed
pip install redis[hiredis]
```

You'll also need a Redis server running. For development, you can use Docker:

```bash
# Start Redis with Docker
docker run -d -p 6379:6379 redis:latest

# Or install Redis locally (macOS)
brew install redis
redis-server
```

## Quick start

```python
from agents import Agent, Runner
from agents.memory.providers.sqlite import SQLiteSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Create a session instance with a session ID
session = SQLiteSession("conversation_123")

# First turn
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# Second turn - agent automatically remembers previous context
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"

# Also works with synchronous runner
result = Runner.run_sync(
    agent,
    "What's the population?",
    session=session
)
print(result.final_output)  # "Approximately 39 million"
```

## How it works

When session memory is enabled:

1. **Before each run**: The runner automatically retrieves the conversation history for the session and prepends it to the input items.
2. **After each run**: All new items generated during the run (user input, assistant responses, tool calls, etc.) are automatically stored in the session.
3. **Context preservation**: Each subsequent run with the same session includes the full conversation history, allowing the agent to maintain context.

This eliminates the need to manually call `.to_input_list()` and manage conversation state between runs.

## Memory operations

### Basic operations

Sessions supports several operations for managing conversation history:

```python
from agents.memory.providers.sqlite import SQLiteSession

session = SQLiteSession("user_123", "conversations.db")

# Get all items in a session
items = await session.get_items()

# Add new items to a session
new_items = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
]
await session.add_items(new_items)

# Remove and return the most recent item
last_item = await session.pop_item()
print(last_item)  # {"role": "assistant", "content": "Hi there!"}

# Clear all items from a session
await session.clear_session()
```

### Using pop_item for corrections

The `pop_item` method is particularly useful when you want to undo or modify the last item in a conversation:

```python
from agents import Agent, Runner
from agents.memory.providers.sqlite import SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("correction_example")

# Initial conversation
result = await Runner.run(
    agent,
    "What's 2 + 2?",
    session=session
)
print(f"Agent: {result.final_output}")

# User wants to correct their question
assistant_item = await session.pop_item()  # Remove agent's response
user_item = await session.pop_item()  # Remove user's question

# Ask a corrected question
result = await Runner.run(
    agent,
    "What's 2 + 3?",
    session=session
)
print(f"Agent: {result.final_output}")
```

## Memory options

### No memory (default)

```python
# Default behavior - no session memory
result = await Runner.run(agent, "Hello")
```

### SQLite memory

```python
from agents.memory.providers.sqlite import SQLiteSession

# In-memory database (lost when process ends)
session = SQLiteSession("user_123")

# Persistent file-based database
session = SQLiteSession("user_123", "conversations.db")

# Use the session
result = await Runner.run(
    agent,
    "Hello",
    session=session
)
```

### Redis memory

```python
from agents.memory.providers.redis import RedisSession

# Basic Redis session (localhost:6379, default database)
session = RedisSession("user_123")

# Redis session with custom configuration
session = RedisSession(
    session_id="user_123",
    redis_url="redis://localhost:6379",
    db=1,
    session_prefix="chat_session",
    messages_prefix="chat_messages",
    ttl=3600  # Session expires after 1 hour
)

# Use the session
result = await Runner.run(
    agent,
    "Hello",
    session=session
)

# Remember to close the Redis connection when done
await session.close()

# Or use async context manager for automatic cleanup
async with RedisSession("user_123") as session:
    result = await Runner.run(
        agent,
        "Hello",
        session=session
    )
    # Connection automatically closed when exiting the context
```

### Redis Session Manager

For production applications with multiple sessions, use the Redis Session Manager for connection pooling:

```python
from agents.memory.providers.redis import RedisSessionManager

# Create a session manager with connection pooling
manager = RedisSessionManager(
    redis_url="redis://localhost:6379",
    db=0,
    default_ttl=7200,  # 2 hours default TTL
    max_connections=10
)

# Get session instances that share the connection pool
session1 = manager.get_session("user_123")
session2 = manager.get_session("user_456", ttl=3600)  # Custom TTL

# Use sessions normally
result1 = await Runner.run(agent, "Hello", session=session1)
result2 = await Runner.run(agent, "Hi there", session=session2)

# List all sessions
session_ids = await manager.list_sessions()
print(f"Active sessions: {session_ids}")

# Delete a specific session
await manager.delete_session("user_123")

# Close the manager and all connections
await manager.close()

# Or use async context manager
async with RedisSessionManager() as manager:
    session = manager.get_session("user_123")
    result = await Runner.run(agent, "Hello", session=session)
    # Connections automatically closed when exiting
```

### Multiple sessions

```python
from agents import Agent, Runner
from agents.memory.providers.sqlite import SQLiteSession

agent = Agent(name="Assistant")

# Different sessions maintain separate conversation histories
session_1 = SQLiteSession("user_123", "conversations.db")
session_2 = SQLiteSession("user_456", "conversations.db")

result1 = await Runner.run(
    agent,
    "Hello",
    session=session_1
)
result2 = await Runner.run(
    agent,
    "Hello",
    session=session_2
)
```

## Custom memory implementations

You can implement your own session memory by creating a class that follows the [`Session`][agents.memory.session.Session] protocol:

```python
from agents.memory import Session
from typing import List

class MyCustomSession:
    """Custom session implementation following the Session protocol."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        # Your initialization here

    async def get_items(self, limit: int | None = None) -> List[dict]:
        """Retrieve conversation history for this session."""
        # Your implementation here
        pass

    async def add_items(self, items: List[dict]) -> None:
        """Store new items for this session."""
        # Your implementation here
        pass

    async def pop_item(self) -> dict | None:
        """Remove and return the most recent item from this session."""
        # Your implementation here
        pass

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        # Your implementation here
        pass

# Use your custom session
from agents import Agent, Runner

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    session=MyCustomSession("my_session")
)
```

## Session management

### Session ID naming

Use meaningful session IDs that help you organize conversations:

-   User-based: `"user_12345"`
-   Thread-based: `"thread_abc123"`
-   Context-based: `"support_ticket_456"`

### Memory persistence

- Use in-memory SQLite (`SQLiteSession("session_id")`) for temporary conversations
- Use file-based SQLite (`SQLiteSession("session_id", "path/to/db.sqlite")`) for persistent conversations
- Use Redis (`RedisSession("session_id")`) for distributed applications and when you need features like automatic expiration
- Consider implementing custom session backends for specialized production systems

### Session management

```python
# Clear a session when conversation should start fresh
await session.clear_session()

# Different agents can share the same session
support_agent = Agent(name="Support")
billing_agent = Agent(name="Billing")

# SQLite session example
from agents.memory.providers.sqlite import SQLiteSession
session = SQLiteSession("user_123")

# Redis session example
from agents.memory.providers.redis import RedisSession
session = RedisSession("user_123", ttl=3600)

# Both agents will see the same conversation history
result1 = await Runner.run(
    support_agent,
    "Help me with my account",
    session=session
)
result2 = await Runner.run(
    billing_agent,
    "What are my charges?",
    session=session
)
````

## Complete example

Here's a complete example showing session memory in action:

```python
import asyncio
from agents import Agent, Runner
from agents.memory.providers.sqlite import SQLiteSession


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Create a session instance that will persist across runs
    session = SQLiteSession("conversation_123", "conversation_history.db")

    print("=== Sessions Example ===")
    print("The agent will remember previous messages automatically.\n")

    # First turn
    print("First turn:")
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Second turn - the agent will remember the previous conversation
    print("Second turn:")
    print("User: What state is it in?")
    result = await Runner.run(
        agent,
        "What state is it in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Third turn - continuing the conversation
    print("Third turn:")
    print("User: What's the population of that state?")
    result = await Runner.run(
        agent,
        "What's the population of that state?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    print("=== Conversation Complete ===")
    print("Notice how the agent remembered the context from previous turns!")
    print("Sessions automatically handles conversation history.")


if __name__ == "__main__":
    asyncio.run(main())
```

## Redis Example

Here's a complete example showing Redis session memory in action:

```python
import asyncio
from agents import Agent, Runner
from agents.memory.providers.redis import RedisSession, RedisSessionManager


async def redis_example():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    print("=== Redis Session Example ===")
    print("Using Redis for distributed session memory.\n")

    # Example 1: Basic Redis session
    async with RedisSession("user_456", ttl=3600) as session:
        print("First turn with Redis:")
        print("User: What's the capital of France?")
        result = await Runner.run(
            agent,
            "What's the capital of France?",
            session=session
        )
        print(f"Assistant: {result.final_output}")
        print()

        print("Second turn:")
        print("User: What's the population?")
        result = await Runner.run(
            agent,
            "What's the population?",
            session=session
        )
        print(f"Assistant: {result.final_output}")
        print()

    # Example 2: Redis Session Manager for production use
    async with RedisSessionManager(default_ttl=7200) as manager:
        # Get multiple sessions that share connection pool
        session1 = manager.get_session("user_123")
        session2 = manager.get_session("user_789")

        # Use sessions concurrently
        result1 = await Runner.run(
            agent,
            "Hello from session 1",
            session=session1
        )
        result2 = await Runner.run(
            agent,
            "Hello from session 2", 
            session=session2
        )

        print("Session Manager Example:")
        print(f"Session 1 response: {result1.final_output}")
        print(f"Session 2 response: {result2.final_output}")
        print()

        # List all active sessions
        session_ids = await manager.list_sessions()
        print(f"Active sessions: {session_ids}")

        # Clean up specific session
        await manager.delete_session("user_789")
        print("Deleted session user_789")

    print("=== Redis Example Complete ===")


if __name__ == "__main__":
    asyncio.run(redis_example())
```

## Choosing the Right Session Backend

### SQLite vs Redis

| Feature | SQLite | Redis |
|---------|--------|-------|
| **Setup** | No additional services required | Requires Redis server |
| **Persistence** | File-based or in-memory | In-memory with optional persistence |
| **Distribution** | Single process only | Multi-process/multi-server |
| **TTL/Expiration** | Manual cleanup required | Automatic expiration with TTL |
| **Concurrency** | Good for single application | Excellent for distributed systems |
| **Performance** | Fast for local access | Very fast, especially for concurrent access |
| **Use Cases** | Single-server applications, development | Production systems, microservices, scaling |

### When to use SQLite:
- Single-server applications
- Development and testing
- When you need file-based persistence
- Simple deployment requirements

### When to use Redis:
- Multi-server applications
- Microservices architecture
- When you need automatic session expiration
- High-concurrency applications
- Distributed systems

## API Reference

For detailed API documentation, see:

- [`Session`][agents.memory.Session] - Protocol interface
- [`SQLiteSession`][agents.memory.providers.sqlite.SQLiteSession] - SQLite implementation
- [`RedisSession`][agents.memory.providers.redis.RedisSession] - Redis implementation
- [`RedisSessionManager`][agents.memory.providers.redis.RedisSessionManager] - Redis connection manager
