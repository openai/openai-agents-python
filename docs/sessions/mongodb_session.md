# MongoDB sessions

`MongoDBSession` stores conversation history in MongoDB, making it a good fit for applications that already use MongoDB or need horizontally-scalable, multi-process session storage.

## Installation

MongoDB sessions require the `mongodb` extra:

```bash
pip install openai-agents[mongodb]
```

This installs `pymongo>=4.14`, which ships the native async API (`AsyncMongoClient`).

## Quick start

### Using a URI

The simplest way to connect is to pass a MongoDB connection URI:

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory import MongoDBSession


async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    session = MongoDBSession.from_uri(
        "user-123",
        uri="mongodb://localhost:27017",
        database="agents",
    )

    result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
    print(result.final_output)  # "San Francisco"

    result = await Runner.run(agent, "What state is it in?", session=session)
    print(result.final_output)  # "California"

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
```

### Using an existing client

If your application already manages an `AsyncMongoClient`, inject it directly so the session shares the same connection pool:

```python
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from agents.extensions.memory import MongoDBSession

client = AsyncMongoClient("mongodb://localhost:27017")
session = MongoDBSession(
    "user-123",
    client=client,
    database="agents",
)
```

When the client is injected externally, calling `session.close()` is a no-op — lifecycle management remains the caller's responsibility.

## MongoDB Atlas

Connect to Atlas the same way, using an `mongodb+srv://` URI:

```python
session = MongoDBSession.from_uri(
    "user-123",
    uri="mongodb+srv://user:password@cluster.example.mongodb.net",
    database="agents",
)
```

## Collection layout

`MongoDBSession` uses two collections (both names are configurable):

| Collection | Default name | Purpose |
|---|---|---|
| Sessions | `agent_sessions` | Session metadata and sequence counter |
| Messages | `agent_messages` | Individual conversation items |

Each message document stores the serialized item and a monotonically increasing `seq` counter that guarantees correct ordering across concurrent writers and processes.

Indexes are created automatically on first use. You can override the collection names:

```python
session = MongoDBSession.from_uri(
    "user-123",
    uri="mongodb://localhost:27017",
    database="myapp",
    sessions_collection="chat_sessions",
    messages_collection="chat_messages",
)
```

## Limiting retrieved history

Use `SessionSettings` to cap how many items are fetched before each run:

```python
from agents import Agent, RunConfig, Runner
from agents.memory import SessionSettings
from agents.extensions.memory import MongoDBSession

agent = Agent(name="Assistant")
session = MongoDBSession.from_uri("user-123", uri="mongodb://localhost:27017")

result = await Runner.run(
    agent,
    "Summarize our recent discussion.",
    session=session,
    run_config=RunConfig(session_settings=SessionSettings(limit=50)),
)
```

## Health check

Use `ping()` to verify connectivity before your first run:

```python
if not await session.ping():
    raise RuntimeError("MongoDB is unreachable")
```

## API reference

- [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession]
- [`Session`][agents.memory.session.Session] — Base session protocol
