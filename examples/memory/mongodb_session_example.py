"""
Example demonstrating MongoDB session memory functionality.

This example shows how to use MongoDB-backed session memory to maintain conversation
history across multiple agent runs, and highlights the features that make MongoDB a
good fit for agent session storage:

- Document model: each message is a BSON document, queryable with the full MongoDB API.
- Atomic per-session sequencing via ``find_one_and_update`` with ``$inc``, safe across
  concurrent writers.
- Compound indexes on ``(session_id, seq)`` for efficient per-session retrieval.
- Shared ``AsyncMongoClient`` across many sessions for production-grade connection pooling.
- Multi-tenant isolation by swapping database or collection names per tenant.
- Aggregation pipelines for analytics on stored conversation history.
- TTL indexes for automatic session expiration.

Note: This example clears the session at the start to ensure a clean demonstration.
In production, you may want to preserve existing conversation history.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from pymongo.asynchronous.mongo_client import AsyncMongoClient

from agents import Agent, Runner
from agents.extensions.memory import MongoDBSession
from agents.items import TResponseInputItem

MONGO_URI = "mongodb://localhost:27017"
DATABASE = "agents_example"


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    print("=== MongoDB Session Example ===")
    print("This example requires MongoDB to be running on localhost:27017")
    print("Start MongoDB with: mongod  (or: docker run -d -p 27017:27017 mongo:7)")
    print()

    # Create a MongoDB session instance
    session_id = "mongodb_conversation_123"
    try:
        session = MongoDBSession.from_uri(
            session_id,
            uri=MONGO_URI,
            database=DATABASE,
        )

        # Test MongoDB connectivity
        if not await session.ping():
            print("MongoDB server is not available!")
            print("Please start MongoDB and try again.")
            return

        print("Connected to MongoDB successfully!")
        print(f"Session ID: {session_id}")

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
        print("MongoDB session automatically handles conversation history with persistence.")

        # Demonstrate session persistence
        print("\n=== Session Persistence Demo ===")
        all_items = await session.get_items()
        print(f"Total messages stored in MongoDB: {len(all_items)}")

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
        new_session = MongoDBSession.from_uri(
            "different_conversation_456",
            uri=MONGO_URI,
            database=DATABASE,
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
        print("Make sure MongoDB is running on localhost:27017")


async def demonstrate_shared_client() -> None:
    """Share one AsyncMongoClient across many sessions (production pattern).

    In a real service you have many concurrent conversations. Creating a new
    ``AsyncMongoClient`` per session would blow up the connection pool. Instead,
    the application owns a single client and injects it into each session.
    """
    print("\n=== Shared AsyncMongoClient Demo ===")

    client: AsyncMongoClient[Any] = AsyncMongoClient(MONGO_URI)
    try:
        try:
            await client.admin.command("ping")
        except Exception:
            print("MongoDB not available, skipping shared-client demo.")
            return

        agent = Agent(name="Support", instructions="Be helpful.")

        # Simulate two concurrent user conversations reusing one client.
        sessions = [
            MongoDBSession(f"shared_client_user_{uid}", client=client, database=DATABASE)
            for uid in ("alice", "bob")
        ]

        await asyncio.gather(
            *(s.clear_session() for s in sessions),
        )

        # Concurrent runs — each session's seq counter is updated atomically on
        # the MongoDB server, so there are no race conditions on ordering.
        await asyncio.gather(
            Runner.run(agent, "Hi, I'm Alice.", session=sessions[0]),
            Runner.run(agent, "Hi, I'm Bob.", session=sessions[1]),
        )

        for s in sessions:
            items = await s.get_items()
            print(f"  {s.session_id}: {len(items)} item(s) stored")

        # Sessions created via the constructor don't own the client, so their
        # .close() is a no-op. The application closes the client itself.
        await asyncio.gather(*(s.clear_session() for s in sessions))
    finally:
        await client.close()


async def demonstrate_multi_tenant_isolation() -> None:
    """Isolate tenants by routing them to different collections.

    MongoDB makes multi-tenancy trivial: pick a database per tenant for hard
    isolation (separate storage, auth, backups), or pick a collection per
    tenant when you want shared infrastructure with logical separation.
    """
    print("\n=== Multi-Tenant Isolation Demo ===")

    client: AsyncMongoClient[Any] = AsyncMongoClient(MONGO_URI)
    try:
        try:
            await client.admin.command("ping")
        except Exception:
            print("MongoDB not available, skipping multi-tenant demo.")
            return

        agent = Agent(name="Support", instructions="Be helpful.")

        def tenant_session(tenant_id: str, user_id: str) -> MongoDBSession:
            # Same database, dedicated collections per tenant. Swap the
            # `database=` arg instead for hard isolation at the database layer.
            return MongoDBSession(
                user_id,
                client=client,
                database=DATABASE,
                sessions_collection=f"{tenant_id}_sessions",
                messages_collection=f"{tenant_id}_messages",
            )

        tenant_a = tenant_session("tenant_a", "user_1")
        tenant_b = tenant_session("tenant_b", "user_1")  # same user id, different tenant

        await asyncio.gather(tenant_a.clear_session(), tenant_b.clear_session())

        await Runner.run(agent, "Secret: tenant A project name is Apollo.", session=tenant_a)
        await Runner.run(agent, "Secret: tenant B project name is Beacon.", session=tenant_b)

        a_items = await tenant_a.get_items()
        b_items = await tenant_b.get_items()
        print(f"  tenant_a collection items: {len(a_items)}")
        print(f"  tenant_b collection items: {len(b_items)}")
        print(
            "  Even though both sessions use the same session_id 'user_1',\n"
            "  their data lives in different collections and never collides."
        )

        await asyncio.gather(tenant_a.clear_session(), tenant_b.clear_session())
    finally:
        await client.close()


async def demonstrate_aggregation_analytics() -> None:
    """Query stored sessions with MongoDB aggregation pipelines.

    Because every message is a real BSON document, you can run the full
    MongoDB query language against your conversation history — no separate
    analytics store needed.
    """
    print("\n=== Aggregation Analytics Demo ===")

    client: AsyncMongoClient[Any] = AsyncMongoClient(MONGO_URI)
    try:
        try:
            await client.admin.command("ping")
        except Exception:
            print("MongoDB not available, skipping analytics demo.")
            return

        agent = Agent(name="Assistant", instructions="Reply in one sentence.")

        # Seed three short conversations so there's something to aggregate over.
        session_ids = ["analytics_a", "analytics_b", "analytics_c"]
        sessions = [MongoDBSession(sid, client=client, database=DATABASE) for sid in session_ids]
        await asyncio.gather(*(s.clear_session() for s in sessions))

        prompts = [
            ("analytics_a", ["Name a planet.", "And another one?"]),
            ("analytics_b", ["Name a primary color."]),
            ("analytics_c", ["Name a fruit.", "And a vegetable?", "And a grain?"]),
        ]
        for sid, turns in prompts:
            s = next(x for x in sessions if x.session_id == sid)
            for turn in turns:
                await Runner.run(agent, turn, session=s)

        # Aggregation pipeline: count messages per session, broken out by role.
        # We parse the stored JSON payload with $function (server-side JS) is
        # overkill here — instead we use $regexMatch on message_data which is
        # fine for a demo. In production you would store role as a dedicated
        # field via a subclassed _serialize_item.
        messages = client[DATABASE]["agent_messages"]
        pipeline: Sequence[Mapping[str, Any]] = [
            {"$match": {"session_id": {"$in": session_ids}}},
            {
                "$group": {
                    "_id": "$session_id",
                    "total_messages": {"$sum": 1},
                    "user_messages": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$regexMatch": {
                                        "input": "$message_data",
                                        "regex": '"role":"user"',
                                    }
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "assistant_messages": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$regexMatch": {
                                        "input": "$message_data",
                                        "regex": '"role":"assistant"',
                                    }
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]

        print("  Per-session message breakdown (from an aggregation pipeline):")
        async for row in await messages.aggregate(pipeline):
            print(
                f"    {row['_id']}: {row['total_messages']} total "
                f"({row['user_messages']} user / {row['assistant_messages']} assistant)"
            )

        await asyncio.gather(*(s.clear_session() for s in sessions))
    finally:
        await client.close()


async def demonstrate_ttl_auto_expiry() -> None:
    """Attach a TTL index so sessions expire automatically.

    MongoDB can garbage-collect stale conversations for you. Add a ``created_at``
    field to session metadata and a TTL index, and the server drops documents
    past their expiry on its own.
    """
    print("\n=== TTL Auto-Expiry Demo ===")

    client: AsyncMongoClient[Any] = AsyncMongoClient(MONGO_URI)
    try:
        try:
            await client.admin.command("ping")
        except Exception:
            print("MongoDB not available, skipping TTL demo.")
            return

        db = client[DATABASE]
        sessions = db["ttl_sessions"]
        messages = db["ttl_messages"]

        # A subclass that stamps ``created_at`` on every message document so
        # the TTL index has a field to expire against.
        class ExpiringMongoDBSession(MongoDBSession):
            async def _serialize_item(self, item: TResponseInputItem) -> str:
                return json.dumps(item, separators=(",", ":"))

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                if not items:
                    return
                await self._ensure_indexes()
                result = await self._sessions.find_one_and_update(
                    {"session_id": self.session_id},
                    {
                        "$setOnInsert": {"session_id": self.session_id},
                        "$inc": {"_seq": len(items)},
                    },
                    upsert=True,
                    return_document=True,
                )
                next_seq = (result["_seq"] if result else len(items)) - len(items)
                now = datetime.now(timezone.utc)
                payload = [
                    {
                        "session_id": self.session_id,
                        "seq": next_seq + i,
                        "message_data": await self._serialize_item(item),
                        "created_at": now,
                    }
                    for i, item in enumerate(items)
                ]
                await self._messages.insert_many(payload, ordered=True)

        # expireAfterSeconds=60 would drop messages after one minute in a real
        # deployment; here we just show how you'd wire it up.
        await messages.create_index("created_at", expireAfterSeconds=60)

        session = ExpiringMongoDBSession(
            "ttl_demo_session",
            client=client,
            database=DATABASE,
            sessions_collection="ttl_sessions",
            messages_collection="ttl_messages",
        )
        await session.clear_session()

        await Runner.run(
            Agent(name="Assistant", instructions="Reply concisely."),
            "This conversation will auto-expire after 60 seconds.",
            session=session,
        )

        doc = await messages.find_one({"session_id": "ttl_demo_session"})
        if doc is not None:
            print(f"  Stored message has created_at={doc['created_at'].isoformat()}")
            print("  MongoDB will drop it automatically once the TTL elapses.")

        await session.clear_session()
        await sessions.drop()
        await messages.drop()
    finally:
        await client.close()


if __name__ == "__main__":
    # To run this example, you need to install the mongodb extras:
    # pip install "openai-agents[mongodb]"
    asyncio.run(main())
    asyncio.run(demonstrate_shared_client())
    asyncio.run(demonstrate_multi_tenant_isolation())
    asyncio.run(demonstrate_aggregation_analytics())
    asyncio.run(demonstrate_ttl_auto_expiry())
