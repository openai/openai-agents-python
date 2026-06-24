"""Serve an Agent over HTTP with the server extension.

Run with:

    pip install "openai-agents[server]"
    python -m examples.agent_server.main

Then try it:

    curl localhost:8000/health
    curl -s localhost:8000/invoke -d '{"input": "What is the capital of France?"}'
    curl -sN localhost:8000/stream -d '{"input": "Tell me a short joke."}'
    # Stateful conversation on a thread:
    curl -s localhost:8000/invoke -d '{"input": "My name is Ada.", "thread_id": "t1"}'
    curl -s localhost:8000/invoke -d '{"input": "What is my name?", "thread_id": "t1"}'
    curl -s localhost:8000/threads/t1
"""

from agents import Agent, SQLiteSession
from agents.extensions.server import AgentServer


def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="You are a concise, helpful assistant.",
    )

    # Persist each thread's history in a SQLite database file.
    server = AgentServer(
        agent,
        session_factory=lambda thread_id: SQLiteSession(thread_id, "agent_server_threads.db"),
    )
    print("Serving agent on http://localhost:8000 (POST /invoke, /stream)")
    server.run(port=8000)


if __name__ == "__main__":
    main()
