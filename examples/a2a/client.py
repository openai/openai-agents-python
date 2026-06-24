"""Call a remote A2A agent.

Start ``examples/a2a/server.py`` first, then run:

    pip install "openai-agents[a2a]"
    python -m examples.a2a.client
"""

import asyncio

from agents.extensions.a2a import A2AClient, Task


async def main() -> None:
    async with A2AClient("http://localhost:8000/") as client:
        card = await client.get_agent_card()
        print(f"Connected to: {card.name} — {card.description}")

        # One-shot send.
        result = await client.send_message("What is the capital of France?")
        if isinstance(result, Task) and result.status.message is not None:
            text = "".join(getattr(part, "text", "") for part in result.status.message.parts)
            print(f"Answer: {text}")

        # Streaming send.
        print("Streaming a second question...")
        async for update in client.stream_message("Name three rivers in Europe."):
            if update.get("kind") == "artifact-update":
                for part in update["artifact"]["parts"]:
                    print(part.get("text", ""), end="", flush=True)
        print()


if __name__ == "__main__":
    asyncio.run(main())
