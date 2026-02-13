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
3. Install openai-agents with dapr in your environment:
        pip install openai-agents[dapr]
4. Use the built-in helper to create components and start containers (Creates ./components with Redis + PostgreSQL and starts containers if Docker is available.):
        python examples/memory/dapr_session_example.py --setup-env --only-setup
5. As always, ensure that the OPENAI_API_KEY environment variable is set.
6. Optionally, if planning on using other Dapr features, run: dapr init
     - This installs Redis, Zipkin, and Placement service locally
     - Useful for workflows, actors, pub/sub, and other Dapr building blocks that are incredible useful for agents.
7. Start dapr sidecar (The app-id is the name of the application that will be running the agent. It can be any name you want. You can check the app-id with `dapr list`.):
        dapr run --app-id openai-agents-example --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components

COMMON ISSUES:
- "Health check connection refused (port 3500)": Always use --dapr-http-port 3500
  when starting Dapr, or set DAPR_HTTP_ENDPOINT="http://localhost:3500"
- "State store not found": Ensure component YAML is in --resources-path directory
- "Dapr sidecar not reachable": Check with `dapr list` and verify gRPC port 50001

Important:
- If you recreate the PostgreSQL container while daprd stays running, the Postgres state store component
  may keep an old connection pool and not re-run initialization, leading to errors like
  "relation \"state\" does not exist". Fix by restarting daprd or triggering a component reload by
  touching the component YAML under your --resources-path.

Note: This example clears the session at the start to ensure a clean demonstration.
In production, you may want to preserve existing conversation history.
"""

import argparse
import asyncio
import os
import shutil
import subprocess
from pathlib import Path

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
DEFAULT_STATE_STORE = os.environ.get("DAPR_STATE_STORE", "statestore")


async def ping_with_retry(
    session: DaprSession, timeout_seconds: float = 5.0, interval_seconds: float = 0.5
) -> bool:
    """Retry session.ping() until success or timeout."""
    now = asyncio.get_running_loop().time
    deadline = now() + timeout_seconds
    while True:
        if await session.ping():
            return True
        print("Dapr sidecar is not available! Retrying...")
        if now() >= deadline:
            return False
        await asyncio.sleep(interval_seconds)


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    print("=== Dapr Session Example ===")
    print()
    print("########################################################")
    print("This example requires Dapr sidecar to be running")
    print("########################################################")
    print()
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
            state_store_name=DEFAULT_STATE_STORE,
            dapr_address=f"localhost:{grpc_port}",
        ) as session:
            # Test Dapr connectivity
            if not await ping_with_retry(session, timeout_seconds=5.0, interval_seconds=0.5):
                print("Dapr sidecar is not available!")
                print("Please start Dapr sidecar and try again.")
                print(
                    "Command: dapr run --app-id myapp --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components"
                )  # noqa: E501
                return

            print("Connected to Dapr successfully!")
            print(f"Session ID: {session_id}")
            print(f"State Store: {DEFAULT_STATE_STORE}")

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
                state_store_name=DEFAULT_STATE_STORE,
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
            state_store_name=DEFAULT_STATE_STORE,
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
            state_store_name=DEFAULT_STATE_STORE,
            dapr_address=f"localhost:{grpc_port}",
            consistency=DAPR_CONSISTENCY_EVENTUAL,
        ) as eventual_session:
            if await eventual_session.ping():
                print("Eventual consistency: Better performance, may have slight delays")
                await eventual_session.add_items([{"role": "user", "content": "Test eventual"}])

        # Strong consistency (guaranteed read-after-write)
        async with DaprSession.from_address(
            "strong_session",
            state_store_name=DEFAULT_STATE_STORE,
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
                state_store_name=DEFAULT_STATE_STORE,
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
    print("\n=== Setup Instructions (Multi-store) ===")
    print("\n1. Create components (Redis + PostgreSQL) in ./components:")
    print("""
# Save as components/statestore-redis.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore-redis
spec:
  type: state.redis
  version: v1
  metadata:
  - name: redisHost
    value: localhost:6379
  - name: redisPassword
    value: ""

# Save as components/statestore-postgres.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore-postgres
spec:
  type: state.postgresql
  version: v2
  metadata:
  - name: connectionString
    value: "host=localhost user=postgres password=postgres dbname=dapr port=5432"
""")
    print("   You can select which one the main demo uses via env var:")
    print("   export DAPR_STATE_STORE=statestore-redis  # or statestore-postgres")
    print("   Start both Redis and PostgreSQL for this multi-store demo:")
    print("   docker run -d -p 6379:6379 redis:7-alpine")
    print(
        "   docker run -d -p 5432:5432 -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=dapr postgres:16-alpine"
    )

    print("\n   NOTE: Always use secret references for passwords/keys in production!")
    print("   See: https://docs.dapr.io/operations/components/component-secrets/")

    print("\n2. Start Dapr sidecar:")
    print(
        "   dapr run --app-id myapp --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path ./components"
    )
    print("\n   IMPORTANT: Always specify --dapr-http-port 3500 to avoid connection errors!")
    print(
        "   If you recreate PostgreSQL while daprd is running, restart daprd or touch the component YAML"
    )
    print(
        "   to trigger a reload, otherwise you may see 'relation "
        + '\\"state\\"'
        + " does not exist'."
    )

    print("\n3. Run this example:")
    print("   python examples/memory/dapr_session_example.py")

    print("\n   Optional: Override store names via env vars:")
    print("   export DAPR_STATE_STORE=statestore-postgres")
    print("   export DAPR_STATE_STORE_REDIS=statestore-redis")
    print("   export DAPR_STATE_STORE_POSTGRES=statestore-postgres")

    print("\n   TIP: If you get 'connection refused' errors, set the HTTP endpoint:")
    print("   export DAPR_HTTP_ENDPOINT='http://localhost:3500'")
    print("   python examples/memory/dapr_session_example.py")

    print("\n4. For Kubernetes deployment:")
    print("   Add these annotations to your pod spec:")
    print("   dapr.io/enabled: 'true'")
    print("   dapr.io/app-id: 'agents-app'")
    print("   Then use: dapr_address='localhost:50001' in your code")

    print("\nDocs: Supported state stores and configuration:")
    print("https://docs.dapr.io/reference/components-reference/supported-state-stores/")


async def demonstrate_multi_store():
    """Demonstrate using two different state stores in the same app."""
    print("\n=== Multi-store Demo (Redis + PostgreSQL) ===")
    redis_store = os.environ.get("DAPR_STATE_STORE_REDIS", "statestore-redis")
    pg_store = os.environ.get("DAPR_STATE_STORE_POSTGRES", "statestore-postgres")

    try:
        async with (
            DaprSession.from_address(
                "multi_store_demo:redis",
                state_store_name=redis_store,
                dapr_address=f"localhost:{grpc_port}",
            ) as redis_session,
            DaprSession.from_address(
                "multi_store_demo:postgres",
                state_store_name=pg_store,
                dapr_address=f"localhost:{grpc_port}",
            ) as pg_session,
        ):
            ok_redis = await ping_with_retry(
                redis_session, timeout_seconds=5.0, interval_seconds=0.5
            )
            ok_pg = await ping_with_retry(pg_session, timeout_seconds=5.0, interval_seconds=0.5)
            if not (ok_redis and ok_pg):
                print(
                    "----------------------------------------\n"
                    "ERROR: One or both state stores are unavailable. Ensure both components exist and are running. \n"
                    "Run with --setup-env to create the components and start the containers.\n"
                    "----------------------------------------\n"
                )
                print(f"Redis store name: {redis_store}")
                print(f"PostgreSQL store name: {pg_store}")
                return

            await redis_session.clear_session()
            await pg_session.clear_session()

            await redis_session.add_items([{"role": "user", "content": "Hello from Redis"}])
            await pg_session.add_items([{"role": "user", "content": "Hello from PostgreSQL"}])

            r_items = await redis_session.get_items()
            p_items = await pg_session.get_items()

            r_example = r_items[-1]["content"] if r_items else "empty"  # type: ignore[typeddict-item]
            p_example = p_items[-1]["content"] if p_items else "empty"  # type: ignore[typeddict-item]

            print(f"{redis_store}: {len(r_items)} items; example: {r_example}")
            print(f"{pg_store}: {len(p_items)} items; example: {p_example}")
            print("Data is isolated per state store.")
    except Exception as e:
        print(f"Multi-store demo error: {e}")


# ------------------------------------------------------------------------------------------------
# ---               Setup Helper Functions                                                      --
# ------------------------------------------------------------------------------------------------


def _write_text_file(path: Path, content: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.write_text(content, encoding="utf-8")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _container_running(name: str):
    if not _docker_available():
        return None
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip().lower() == "true"
    except Exception:
        return None


def _ensure_container(name: str, run_args: list[str]) -> None:
    if not _docker_available():
        raise SystemExit(
            "Docker is required to automatically start containers for '"
            + name
            + "'.\nInstall Docker: https://docs.docker.com/get-docker/\n"
            + "Alternatively, start the container manually and re-run with --setup-env."
        )
    status = _container_running(name)
    if status is True:
        print(f"Container '{name}' already running.")
        return
    if status is False:
        subprocess.run(["docker", "start", name], check=False)
        print(f"Started existing container '{name}'.")
        return
    subprocess.run(["docker", "run", "-d", "--name", name, *run_args], check=False)
    print(f"Created and started container '{name}'.")


def setup_environment(components_dir: str = "./components", overwrite: bool = False) -> None:
    """Create Redis/PostgreSQL component files and start containers if available."""
    components_path = Path(components_dir)
    components_path.mkdir(parents=True, exist_ok=True)

    redis_component = """
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore-redis
spec:
  type: state.redis
  version: v1
  metadata:
  - name: redisHost
    value: localhost:6379
  - name: redisPassword
    value: ""
""".lstrip()

    postgres_component = """
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore-postgres
spec:
  type: state.postgresql
  version: v2
  metadata:
  - name: connectionString
    value: "host=localhost user=postgres password=postgres dbname=dapr port=5432"
""".lstrip()

    default_component = """
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
""".lstrip()

    _write_text_file(components_path / "statestore-redis.yaml", redis_component, overwrite)
    _write_text_file(components_path / "statestore-postgres.yaml", postgres_component, overwrite)
    _write_text_file(components_path / "statestore.yaml", default_component, overwrite)

    print(f"Components written under: {components_path.resolve()}")

    _ensure_container("dapr_redis", ["-p", "6379:6379", "redis:7-alpine"])
    _ensure_container(
        "dapr_postgres",
        [
            "-p",
            "5432:5432",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=dapr",
            "postgres:16-alpine",
        ],
    )
    print("Environment setup complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dapr session example")
    parser.add_argument(
        "--setup-env",
        action="store_true",
        help="Create ./components and add Redis/PostgreSQL components; start containers if possible.",
    )
    parser.add_argument(
        "--components-dir",
        default="./components",
        help="Path to Dapr components directory (default: ./components)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing component files if present.",
    )
    parser.add_argument(
        "--only-setup",
        action="store_true",
        help="Exit after setting up the environment.",
    )
    args = parser.parse_args()

    if args.setup_env:
        setup_environment(args.components_dir, overwrite=args.overwrite)
        if args.only_setup:
            raise SystemExit(0)

    asyncio.run(setup_instructions())
    asyncio.run(main())
    asyncio.run(demonstrate_advanced_features())
    asyncio.run(demonstrate_multi_store())
