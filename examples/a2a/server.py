"""Publish an Agent as an A2A endpoint.

Run with:

    pip install "openai-agents[a2a]"
    python -m examples.a2a.server

Then, in another terminal, run ``examples/a2a/client.py`` to call it.
"""

from agents import Agent
from agents.extensions.a2a import A2AServer


def main() -> None:
    agent = Agent(
        name="Geography Assistant",
        instructions="You answer geography questions concisely.",
    )
    server = A2AServer(agent, url="http://localhost:8000/")
    print("Serving A2A agent on http://localhost:8000/")
    print("Agent card: http://localhost:8000/.well-known/agent-card.json")
    server.run(port=8000)


if __name__ == "__main__":
    main()
