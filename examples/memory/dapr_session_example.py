"""
Example demonstrating Dapr State Store session memory functionality.

This example shows how to use Dapr-backed session memory to maintain conversation
history across multiple agent runs with support for various backend stores
(Redis, PostgreSQL, MongoDB, etc.).

WHAT IS DAPR?
Dapr (https://dapr.io) is a portable, event-driven runtime that simplifies building
resilient applications. Its state management building block provides a unified API
for storing data across 30+ databases with built-in telemetry, tracing, encryption, data
isolation and lifecycle management via time-to-live (TTL). See: https://docs.dapr.io/developing-applications/building-blocks/state-management/

WHEN TO USE DaprSession:
- Horizontally scaled deployments (multiple agent instances behind a load balancer)
- Multi-region requirements (agents run in different geographic regions)
- Existing Dapr adoption (your team already uses Dapr for other services)
- Backend flexibility (switch state stores without code changes)
- Enterprise governance (centralized control over state management policies)

WHEN TO CONSIDER ALTERNATIVES:
- Use SQLiteSession for single-instance agents (desktop app, CLI tool)
- Use Session (in-memory) for quick prototypes or short-lived sessions

PRODUCTION FEATURES (provided by Dapr):
- Backend flexibility: 30+ state stores (Redis, PostgreSQL, MongoDB, Cosmos DB, etc.)
- Built-in observability: Distributed tracing, metrics, telemetry (zero code)
- Data isolation: App-level or namespace-level state scoping for multi-tenancy
- TTL support: Automatic session expiration (store-dependent)
- Consistency levels: Eventual (faster) or strong (read-after-write guarantee)
- State encryption: AES-GCM encryption at the Dapr component level
- Cloud-native: Seamless Kubernetes integration (Dapr runs as sidecar)
- Cloud Service Provider (CSP) native authentication and authorization support.

PREREQUISITES:
1. Install Dapr CLI: https://docs.dapr.io/getting-started/install-dapr-cli/
2. Install Docker (for running Redis and optionally Dapr containers)
3. Choose one of the following setups:

   Option A - Full Dapr environment (recommended if you plan to use other Dapr features):
     - Run: dapr init
     - This installs Redis, Zipkin, and Placement service locally
     - Useful for workflows, actors, pub/sub, and other Dapr building blocks

   Option B - Minimal setup (just for DaprSession):
     - Start Redis only: docker run -d -p 6379:6379 redis:7-alpine
     - Requires only Dapr CLI (no dapr init needed)

4. Create components directory with statestore.yaml configuration (see setup_instructions())
5. As always, ensure that the OPENAI_API_KEY environment variable is set.

COMMON ISSUES:
- "Health check connection refused (port 3500)": Always use --dapr-http-port 3500
  when starting Dapr, or set DAPR_HTTP_ENDPOINT="http://localhost:3500"
- "State store not found": Ensure component YAML is in --resources-path directory
- "Dapr sidecar not reachable": Check with `dapr list` and verify gRPC port 50001

Note: This example clears the session at the start to ensure a clean demonstration.
In production, you may want to preserve existing conversation history.
"""

import asyncio
import os

os.environ["GRPC_VERBOSITY"] = (
    "ERROR"  # Suppress gRPC warnings caused by the Dapr Python SDK gRPC connection.
)

from agents import Agent, Runner
from agents.extensions.memory import (
    DAPR_CONSISTENCY_EVENTUAL,
    DAPR_CONSISTENCY_STRONG,
    DaprSession,
)

grpc_port = os.environ.get("DAPR_GRPC_PORT", "50001")


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    print("=== Dapr Session Example ===")
    print("This example requires Dapr sidecar to be running")
    print(
        "Start Dapr with: dapr run --app-id myapp --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components"
    )  # noqa: E501
    print()

    # Create a Dapr session instance with context manager for automatic cleanup
    session_id = "dapr_conversation_123"
    try:
        # Use async with to automatically close the session on exit
        async with DaprSession.from_address(
            session_id,
            state_store_name="statestore",
            dapr_address=f"localhost:{grpc_port}",
        ) as session:
            # Test Dapr connectivity
            if not await session.ping():
                print("Dapr sidecar is not available!")
                print("Please start Dapr sidecar and try again.")
                print(
                    "Command: dapr run --app-id myapp --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components"
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
            print(
                "Dapr session automatically handles conversation history with backend flexibility."
            )

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
            # Use context manager for the new session too
            async with DaprSession.from_address(
                "different_conversation_456",
                state_store_name="statestore",
                dapr_address=f"localhost:{grpc_port}",
            ) as new_session:
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
                # No need to call close() - context manager handles it automatically!

    except Exception as e:
        print(f"Error: {e}")
        print(
            "Make sure Dapr sidecar is running with: dapr run --app-id myapp --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components"
        )  # noqa: E501


async def demonstrate_advanced_features():
    """Demonstrate advanced Dapr session features."""
    print("\n=== Advanced Features Demo ===")

    try:
        # TTL (time-to-live) configuration
        print("\n1. TTL Configuration:")
        async with DaprSession.from_address(
            "ttl_demo_session",
            state_store_name="statestore",
            dapr_address=f"localhost:{grpc_port}",
            ttl=3600,  # 1 hour TTL
        ) as ttl_session:
            if await ttl_session.ping():
                await Runner.run(
                    Agent(name="Assistant", instructions="Be helpful"),
                    "This message will expire in 1 hour",
                    session=ttl_session,
                )
                print("Created session with 1-hour TTL - messages will auto-expire")
                print("(TTL support depends on the underlying state store)")

        # Consistency levels
        print("\n2. Consistency Levels:")

        # Eventual consistency (better performance)
        async with DaprSession.from_address(
            "eventual_session",
            state_store_name="statestore",
            dapr_address=f"localhost:{grpc_port}",
            consistency=DAPR_CONSISTENCY_EVENTUAL,
        ) as eventual_session:
            if await eventual_session.ping():
                print("Eventual consistency: Better performance, may have slight delays")
                await eventual_session.add_items([{"role": "user", "content": "Test eventual"}])

        # Strong consistency (guaranteed read-after-write)
        async with DaprSession.from_address(
            "strong_session",
            state_store_name="statestore",
            dapr_address=f"localhost:{grpc_port}",
            consistency=DAPR_CONSISTENCY_STRONG,
        ) as strong_session:
            if await strong_session.ping():
                print("Strong consistency: Guaranteed immediate consistency")
                await strong_session.add_items([{"role": "user", "content": "Test strong"}])

        # Multi-tenancy example
        print("\n3. Multi-tenancy with Session Prefixes:")

        def get_tenant_session(tenant_id: str, user_id: str) -> DaprSession:
            session_id = f"{tenant_id}:{user_id}"
            return DaprSession.from_address(
                session_id,
                state_store_name="statestore",
                dapr_address=f"localhost:{grpc_port}",
            )

        async with get_tenant_session("tenant-a", "user-123") as tenant_a_session:
            async with get_tenant_session("tenant-b", "user-123") as tenant_b_session:
                if await tenant_a_session.ping() and await tenant_b_session.ping():
                    await tenant_a_session.add_items([{"role": "user", "content": "Tenant A data"}])
                    await tenant_b_session.add_items([{"role": "user", "content": "Tenant B data"}])
                    print("Multi-tenant sessions created with isolated data")

    except Exception as e:
        print(f"Advanced features error: {e}")


async def setup_instructions():
    """Print setup instructions for running the example."""
    print("\n=== Setup Instructions ===")
    print("\n1. Create a components directory:")
    print("   mkdir -p components")
    print("\n2. Create statestore.yaml with your chosen state store:")
    print("\n   OPTION A - Redis (recommended for getting started):")
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
    print("   Start Redis: docker run -d -p 6379:6379 redis:7-alpine")
    print("   (Skip if you already ran 'dapr init' - it installs Redis locally)")

    print("\n   OPTION B - PostgreSQL (v2 recommended):")
    print("""
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.postgresql
  version: v2
  metadata:
  - name: connectionString
    value: "host=localhost user=postgres password=postgres dbname=dapr port=5432"
""")
    print(
        "   See: https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-postgresql-v2/"
    )

    print("\n   OPTION C - MongoDB:")
    print("""
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.mongodb
  version: v1
  metadata:
  - name: host
    value: "localhost:27017"
""")
    print(
        "   See: https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-mongodb/"
    )

    print("\n   OPTION D - Azure Cosmos DB:")
    print("""
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.azure.cosmosdb
  version: v1
  metadata:
  - name: url
    value: "https://<your-account>.documents.azure.com:443/"
  - name: masterKey
    value: "<your-master-key>"
  - name: database
    value: "dapr"
""")
    print(
        "   See: https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-azure-cosmosdb/"
    )

    print("\n   NOTE: Always use secret references for passwords/keys in production!")
    print("   See: https://docs.dapr.io/operations/components/component-secrets/")

    print("\n3. Start Dapr sidecar:")
    print(
        "   dapr run --app-id myapp --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components"
    )
    print("\n   IMPORTANT: Always specify --dapr-http-port 3500 to avoid connection errors!")

    print("\n4. Run this example:")
    print("   python examples/memory/dapr_session_example.py")

    print("\n   TIP: If you get 'connection refused' errors, set the HTTP endpoint:")
    print("   export DAPR_HTTP_ENDPOINT='http://localhost:3500'")
    print("   python examples/memory/dapr_session_example.py")

    print("\n5. For Kubernetes deployment:")
    print("   Add these annotations to your pod spec:")
    print("   dapr.io/enabled: 'true'")
    print("   dapr.io/app-id: 'agents-app'")
    print("   Then use: dapr_address='localhost:50001' in your code")

    print("\nFull list of 30+ supported state stores:")
    print("https://docs.dapr.io/reference/components-reference/supported-state-stores/")


if __name__ == "__main__":
    asyncio.run(setup_instructions())
    asyncio.run(main())
    asyncio.run(demonstrate_advanced_features())
