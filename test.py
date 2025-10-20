import asyncio
import os
os.environ["GRPC_VERBOSITY"] = (
    "ERROR"  # Suppress gRPC warnings caused by the Dapr Python SDK gRPC connection.
)
from agents import Agent, Runner
from agents.extensions.memory import DaprSession

grpc_port = os.environ.get("DAPR_GRPC_PORT", "50001")


async def main():
    # Create agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Connect to Dapr sidecar (using default gRPC port 50001)
    session = DaprSession.from_address(
        session_id="user-123",
        state_store_name="statestore",
        dapr_address=f"localhost:{grpc_port}",
    )

    try:
        # First turn
        result = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session
        )
        print(f"Agent: {result.final_output}")  # "San Francisco"

        # Second turn - agent remembers context
        result = await Runner.run(
            agent,
            "What state is it in?",
            session=session
        )
        print(f"Agent: {result.final_output}")  # "California"

    finally:
        # Always clean up
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())