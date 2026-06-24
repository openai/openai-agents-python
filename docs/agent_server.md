# Agent server

The server extension turns an [`Agent`][agents.agent.Agent] into an HTTP service with invoke, streaming, and thread (session) endpoints — so you can deploy an agent without hand-writing the web layer, SSE serialization, and per-thread session wiring.

Install the optional dependencies:

```bash
pip install "openai-agents[server]"
```

## Quickstart

```python
from agents import Agent
from agents.extensions.server import AgentServer

agent = Agent(name="Assistant", instructions="You are a helpful assistant.")

server = AgentServer(agent)
server.run(port=8000)  # requires `uvicorn`
```

`server.app` is a plain FastAPI application, so you can also mount it inside an existing service or serve it with any ASGI server.

## Endpoints

| Method & path | Description |
| --- | --- |
| `POST /invoke` | Run the agent. Body: `{"input": "...", "thread_id": "optional"}`. Returns `{"output": ..., "thread_id": ...}`. |
| `POST /stream` | Same body; returns Server-Sent Events (`text_delta`, run-item events, then a terminal `done` event with the final output). |
| `GET /threads/{id}` | Return the thread's persisted items (requires a session factory). |
| `DELETE /threads/{id}` | Clear the thread's history. |
| `GET /health` | Readiness probe. |

## Threads (sessions)

Pass a `session_factory` to persist conversation history per `thread_id`. Any [`Session`][agents.memory.session.Session] implementation works:

```python
from agents.extensions.memory import SQLAlchemySession
from agents.extensions.server import AgentServer

server = AgentServer(
    agent,
    session_factory=lambda thread_id: SQLAlchemySession(
        thread_id, engine=engine, create_tables=True
    ),
)
```

When no `session_factory` is configured, requests run statelessly and the thread endpoints return `400`.

## Security

- Set `api_key=...` to require an `Authorization: Bearer <key>` or `X-API-Key: <key>` header on every request.
- `run()` refuses to bind a non-loopback host (e.g. `0.0.0.0`) when no `api_key` is configured, to avoid accidentally exposing an unauthenticated agent on the network.

## API reference

::: agents.extensions.server
