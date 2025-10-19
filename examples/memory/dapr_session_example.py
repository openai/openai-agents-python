"""
Example demonstrating Dapr State Store session memory functionality.

This example shows how to use Dapr-backed session memory to maintain conversation
history across multiple agent runs with support for various backend stores
(Redis, PostgreSQL, MongoDB, etc.).

Prerequisites:
1. Install Dapr CLI: https://docs.dapr.io/getting-started/install-dapr-cli/
2. Initialize Dapr: dapr init
3. Start Redis: docker run -d -p 6379:6379 redis:7-alpine
4. Create components directory with statestore.yaml configuration

Note: This example clears the session at the start to ensure a clean demonstration.
In production, you may want to preserve existing conversation history.
"""

import asyncio

from agents import Agent, Runner
from agents.extensions.memory import DaprSession


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    print("=== Dapr Session Example ===")
    print("This example requires Dapr sidecar to be running")
    print(
        "Start Dapr with: dapr run --app-id myapp --dapr-grpc-port 50001 --components-path ./components"
    )  # noqa: E501
    print()

    # Create a Dapr session instance
    session_id = "dapr_conversation_123"
    try:
        session = DaprSession.from_address(
            session_id,
            state_store_name="statestore",
            dapr_address="localhost:50001",
        )

        # Test Dapr connectivity
        if not await session.ping():
            print("Dapr sidecar is not available!")
            print("Please start Dapr sidecar and try again.")
            print(
                "Command: dapr run --app-id myapp --dapr-grpc-port 50001 --components-path ./components"
            )  # noqa: E501
            return

        print("Connected to Dapr successfully!")
        print(f"Session ID: {session_id}")
        print("State Store: statestore")

        # Clear any existing session data for a clean start
        await session.clear_session()
        print("Session cleared for clean demonstration.")
        print("The agent will remember previous messages automatically.\n")

        # First turn
        print("First turn:")
        print("User: What city is the Golden Gate Bridge in?")
        result = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        print(f"Assistant: {result.final_output}")
        print()

        # Second turn - the agent will remember the previous conversation
        print("Second turn:")
        print("User: What state is it in?")
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(f"Assistant: {result.final_output}")
        print()

        # Third turn - continuing the conversation
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
        print("Dapr session automatically handles conversation history with backend flexibility.")

        # Demonstrate session persistence
        print("\n=== Session Persistence Demo ===")
        all_items = await session.get_items()
        print(f"Total messages stored in Dapr: {len(all_items)}")

        # Demonstrate the limit parameter
        print("\n=== Latest Items Demo ===")
        latest_items = await session.get_items(limit=2)
        print("Latest 2 items:")
        for i, msg in enumerate(latest_items, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            print(f"  {i}. {role}: {content}")

        # Demonstrate session isolation with a new session
        print("\n=== Session Isolation Demo ===")
        new_session = DaprSession.from_address(
            "different_conversation_456",
            state_store_name="statestore",
            dapr_address="localhost:50001",
        )

        print("Creating a new session with different ID...")
        result = await Runner.run(
            agent,
            "Hello, this is a new conversation!",
            session=new_session,
        )
        print(f"New session response: {result.final_output}")

        # Show that sessions are isolated
        original_items = await session.get_items()
        new_items = await new_session.get_items()
        print(f"Original session has {len(original_items)} items")
        print(f"New session has {len(new_items)} items")
        print("Sessions are completely isolated!")

        # Clean up the new session
        await new_session.clear_session()
        await new_session.close()

        # Close the main session
        await session.close()

    except Exception as e:
        print(f"Error: {e}")
        print(
            "Make sure Dapr sidecar is running with: dapr run --app-id myapp --dapr-grpc-port 50001 --components-path ./components"
        )  # noqa: E501


async def demonstrate_advanced_features():
    """Demonstrate advanced Dapr session features."""
    print("\n=== Advanced Features Demo ===")

    try:
        # TTL (time-to-live) configuration
        print("\n1. TTL Configuration:")
        ttl_session = DaprSession.from_address(
            "ttl_demo_session",
            state_store_name="statestore",
            dapr_address="localhost:50001",
            ttl=3600,  # 1 hour TTL
        )

        if await ttl_session.ping():
            await Runner.run(
                Agent(name="Assistant", instructions="Be helpful"),
                "This message will expire in 1 hour",
                session=ttl_session,
            )
            print("Created session with 1-hour TTL - messages will auto-expire")
            print("(TTL support depends on the underlying state store)")

        await ttl_session.close()

        # Consistency levels
        print("\n2. Consistency Levels:")

        # Eventual consistency (better performance)
        eventual_session = DaprSession.from_address(
            "eventual_session",
            state_store_name="statestore",
            dapr_address="localhost:50001",
            consistency="eventual",
        )

        # Strong consistency (guaranteed read-after-write)
        strong_session = DaprSession.from_address(
            "strong_session",
            state_store_name="statestore",
            dapr_address="localhost:50001",
            consistency="strong",
        )

        if await eventual_session.ping():
            print("Eventual consistency: Better performance, may have slight delays")
            await eventual_session.add_items([{"role": "user", "content": "Test eventual"}])

        if await strong_session.ping():
            print("Strong consistency: Guaranteed immediate consistency")
            await strong_session.add_items([{"role": "user", "content": "Test strong"}])

        await eventual_session.close()
        await strong_session.close()

        # Multi-tenancy example
        print("\n3. Multi-tenancy with Session Prefixes:")

        def get_tenant_session(tenant_id: str, user_id: str) -> DaprSession:
            session_id = f"{tenant_id}:{user_id}"
            return DaprSession.from_address(
                session_id,
                state_store_name="statestore",
                dapr_address="localhost:50001",
            )

        tenant_a_session = get_tenant_session("tenant-a", "user-123")
        tenant_b_session = get_tenant_session("tenant-b", "user-123")

        if await tenant_a_session.ping() and await tenant_b_session.ping():
            await tenant_a_session.add_items([{"role": "user", "content": "Tenant A data"}])
            await tenant_b_session.add_items([{"role": "user", "content": "Tenant B data"}])
            print("Multi-tenant sessions created with isolated data")

        await tenant_a_session.close()
        await tenant_b_session.close()

    except Exception as e:
        print(f"Advanced features error: {e}")


async def setup_instructions():
    """Print setup instructions for running the example."""
    print("\n=== Setup Instructions ===")
    print("\n1. Create a components directory:")
    print("   mkdir -p components")
    print("\n2. Create statestore.yaml with Redis configuration:")
    print("""
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.redis
  version: v1
  metadata:
  - name: redisHost
    value: localhost:6379
  - name: redisPassword
    value: ""
""")
    print("\n3. Start Redis:")
    print("   docker run -d -p 6379:6379 redis:7-alpine")
    print("\n4. Start Dapr sidecar:")
    print("   dapr run --app-id myapp --dapr-grpc-port 50001 --components-path ./components")
    print("\n5. Run this example:")
    print("   python examples/memory/dapr_session_example.py")
    print("\nAlternatively, you can use other state stores supported by Dapr:")
    print("- PostgreSQL: state.postgresql")
    print("- MongoDB: state.mongodb")
    print("- Azure Cosmos DB: state.azure.cosmosdb")
    print("- AWS DynamoDB: state.aws.dynamodb")
    print("See: https://docs.dapr.io/reference/components-reference/supported-state-stores/")


if __name__ == "__main__":
    asyncio.run(setup_instructions())
    asyncio.run(main())
    asyncio.run(demonstrate_advanced_features())
