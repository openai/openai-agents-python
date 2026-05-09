"""Tests for MongoDBSession using in-process mock objects.

All tests run without a real MongoDB server — or even the ``pymongo``
package — by injecting lightweight fake classes into ``sys.modules``
before the module under test is imported.  This keeps the suite fast and
dependency-free while exercising the full session logic.
"""

from __future__ import annotations

import sys
import types
from collections import defaultdict
from typing import Any
from unittest.mock import patch

import pytest

from agents import Agent, Runner, TResponseInputItem
from agents.memory.session_settings import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# In-memory fake pymongo async types
# ---------------------------------------------------------------------------


class FakeObjectId:
    """Minimal ObjectId stand-in with a monotonic counter for sort order."""

    _counter = 0

    def __init__(self) -> None:
        FakeObjectId._counter += 1
        self._value = FakeObjectId._counter

    def __lt__(self, other: FakeObjectId) -> bool:
        return self._value < other._value

    def __repr__(self) -> str:
        return f"FakeObjectId({self._value})"


class FakeCursor:
    """Minimal async cursor returned by ``find()``."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def sort(
        self,
        key: str | list[tuple[str, int]],
        direction: int | None = None,
    ) -> FakeCursor:
        if isinstance(key, list):
            pairs = key
        else:
            direction = direction if direction is not None else 1
            pairs = [(key, direction)]

        docs = list(self._docs)
        for field, dir_ in reversed(pairs):
            docs.sort(key=lambda d: d.get(field, 0), reverse=(dir_ == -1))
        self._docs = docs
        return self

    def limit(self, n: int) -> FakeCursor:
        self._docs = self._docs[:n]
        return self

    async def to_list(self) -> list[dict[str, Any]]:
        return list(self._docs)


class FakeAsyncCollection:
    """In-memory substitute for pymongo AsyncCollection."""

    def __init__(self) -> None:
        self._docs: dict[Any, dict[str, Any]] = {}

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        return "fake_index"

    def find(self, query: dict[str, Any] | None = None) -> FakeCursor:
        query = query or {}
        results = [doc for doc in self._docs.values() if self._matches(doc, query)]
        return FakeCursor(results)

    async def find_one_and_delete(
        self,
        query: dict[str, Any],
        sort: list[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        matches = [doc for doc in self._docs.values() if self._matches(doc, query)]
        if not matches:
            return None
        if sort:
            field, dir_ = sort[0]
            matches.sort(key=lambda d: d.get(field, 0), reverse=(dir_ == -1))
        doc = matches[0]
        self._docs.pop(id(doc["_id"]))
        return doc

    async def insert_many(
        self,
        documents: list[dict[str, Any]],
        ordered: bool = True,
    ) -> Any:
        for doc in documents:
            if "_id" not in doc:
                doc["_id"] = FakeObjectId()
            self._docs[id(doc["_id"])] = dict(doc)

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
        return_document: bool = False,
    ) -> dict[str, Any] | None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                # Apply $inc fields.
                for field, delta in update.get("$inc", {}).items():
                    doc[field] = doc.get(field, 0) + delta
                return dict(doc) if return_document else None
        if upsert:
            new_doc: dict[str, Any] = {"_id": FakeObjectId()}
            new_doc.update(update.get("$setOnInsert", {}))
            for field, delta in update.get("$inc", {}).items():
                new_doc[field] = new_doc.get(field, 0) + delta
            self._docs[id(new_doc["_id"])] = new_doc
            return dict(new_doc) if return_document else None
        return None

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                return  # Exists — $setOnInsert is a no-op on existing docs.
        if upsert:
            new_doc2: dict[str, Any] = {"_id": FakeObjectId()}
            new_doc2.update(update.get("$setOnInsert", {}))
            self._docs[id(new_doc2["_id"])] = new_doc2

    async def delete_many(self, query: dict[str, Any]) -> None:
        to_remove = [k for k, d in self._docs.items() if self._matches(d, query)]
        for key in to_remove:
            del self._docs[key]

    async def delete_one(self, query: dict[str, Any]) -> None:
        for key, doc in list(self._docs.items()):
            if self._matches(doc, query):
                del self._docs[key]
                return

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(doc.get(k) == v for k, v in query.items())


class FakeAsyncDatabase:
    """In-memory substitute for a pymongo async Database."""

    def __init__(self) -> None:
        self._collections: dict[str, FakeAsyncCollection] = defaultdict(FakeAsyncCollection)

    def __getitem__(self, name: str) -> FakeAsyncCollection:
        return self._collections[name]


class FakeAdminDatabase:
    """Minimal admin database used by ping()."""

    def __init__(self) -> None:
        self._closed = False

    async def command(self, cmd: str) -> dict[str, Any]:
        if self._closed:
            raise ConnectionError("Client is closed.")
        return {"ok": 1}


class FakeDriverInfo:
    """Minimal stand-in for pymongo.driver_info.DriverInfo."""

    def __init__(self, name: str, version: str | None = None) -> None:
        self.name = name
        self.version = version


class FakeAsyncMongoClient:
    """In-memory substitute for pymongo AsyncMongoClient."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._databases: dict[str, FakeAsyncDatabase] = defaultdict(FakeAsyncDatabase)
        self._closed = False
        self.admin = FakeAdminDatabase()
        self._metadata_calls: list[FakeDriverInfo] = []

    def __getitem__(self, name: str) -> FakeAsyncDatabase:
        return self._databases[name]

    def append_metadata(self, driver_info: FakeDriverInfo) -> None:
        """Record append_metadata calls for test assertions."""
        self._metadata_calls.append(driver_info)

    async def close(self) -> None:
        """Async close — matches PyMongo's AsyncMongoClient.close() signature."""
        self._closed = True
        self.admin._closed = True


# ---------------------------------------------------------------------------
# Inject fake pymongo into sys.modules before importing the module under test
# ---------------------------------------------------------------------------


def _make_fake_pymongo_modules() -> None:
    """Populate sys.modules with stub pymongo async modules."""
    pymongo_mod = sys.modules.get("pymongo") or types.ModuleType("pymongo")

    async_pkg = types.ModuleType("pymongo.asynchronous")
    collection_mod = types.ModuleType("pymongo.asynchronous.collection")
    client_mod = types.ModuleType("pymongo.asynchronous.mongo_client")
    driver_info_mod = types.ModuleType("pymongo.driver_info")

    collection_mod.AsyncCollection = FakeAsyncCollection  # type: ignore[attr-defined]
    client_mod.AsyncMongoClient = FakeAsyncMongoClient  # type: ignore[attr-defined]
    driver_info_mod.DriverInfo = FakeDriverInfo  # type: ignore[attr-defined]

    sys.modules["pymongo"] = pymongo_mod
    sys.modules["pymongo.asynchronous"] = async_pkg
    sys.modules["pymongo.asynchronous.collection"] = collection_mod
    sys.modules["pymongo.asynchronous.mongo_client"] = client_mod
    sys.modules["pymongo.driver_info"] = driver_info_mod


_make_fake_pymongo_modules()

# Now it's safe to import the module under test.
from agents.extensions.memory.mongodb_session import MongoDBSession  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_session(session_id: str = "test-session", **kwargs: Any) -> MongoDBSession:
    """Create a MongoDBSession backed by a FakeAsyncMongoClient."""
    client = FakeAsyncMongoClient()
    MongoDBSession._init_state.clear()
    return MongoDBSession(
        session_id,
        client=client,  # type: ignore[arg-type]
        database="agents_test",
        **kwargs,
    )


@pytest.fixture
def session() -> MongoDBSession:
    return _make_session()


@pytest.fixture
def agent() -> Agent:
    return Agent(name="test", model=FakeModel())


# ---------------------------------------------------------------------------
# Core CRUD tests
# ---------------------------------------------------------------------------


async def test_add_and_get_items(session: MongoDBSession) -> None:
    """Items added to the session are retrievable in insertion order."""
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"


async def test_add_empty_list_is_noop(session: MongoDBSession) -> None:
    """Adding an empty list must not create any documents."""
    await session.add_items([])
    assert await session.get_items() == []


async def test_get_items_empty_session(session: MongoDBSession) -> None:
    """Retrieving items from a brand-new session returns an empty list."""
    assert await session.get_items() == []


async def test_pop_item_returns_last(session: MongoDBSession) -> None:
    """pop_item must return and remove the most recently added item."""
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    await session.add_items(items)

    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "second"

    remaining = await session.get_items()
    assert len(remaining) == 1
    assert remaining[0].get("content") == "first"


async def test_pop_item_empty_session(session: MongoDBSession) -> None:
    """pop_item on an empty session must return None."""
    assert await session.pop_item() is None


async def test_clear_session(session: MongoDBSession) -> None:
    """clear_session must remove all items and session metadata."""
    await session.add_items([{"role": "user", "content": "x"}])
    await session.clear_session()
    assert await session.get_items() == []


async def test_multiple_add_calls_accumulate(session: MongoDBSession) -> None:
    """Items from separate add_items calls all appear in get_items."""
    await session.add_items([{"role": "user", "content": "a"}])
    await session.add_items([{"role": "assistant", "content": "b"}])
    await session.add_items([{"role": "user", "content": "c"}])

    items = await session.get_items()
    assert [i.get("content") for i in items] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Limit / SessionSettings tests
# ---------------------------------------------------------------------------


async def test_get_items_with_explicit_limit(session: MongoDBSession) -> None:
    """Explicit limit returns the N most recent items in chronological order."""
    await session.add_items([{"role": "user", "content": str(i)} for i in range(6)])

    result = await session.get_items(limit=3)
    assert len(result) == 3
    assert [r.get("content") for r in result] == ["3", "4", "5"]


async def test_get_items_limit_zero(session: MongoDBSession) -> None:
    """A limit of 0 must return an empty list immediately."""
    await session.add_items([{"role": "user", "content": "x"}])
    assert await session.get_items(limit=0) == []


async def test_get_items_limit_exceeds_count(session: MongoDBSession) -> None:
    """Requesting more items than exist returns all items without error."""
    await session.add_items([{"role": "user", "content": "only"}])
    result = await session.get_items(limit=100)
    assert len(result) == 1


async def test_session_settings_limit_used_as_default() -> None:
    """session_settings.limit is applied when no explicit limit is given."""
    MongoDBSession._init_state.clear()
    s = MongoDBSession(
        "ls-test",
        client=FakeAsyncMongoClient(),  # type: ignore[arg-type]
        database="agents_test",
        session_settings=SessionSettings(limit=2),
    )
    await s.add_items([{"role": "user", "content": str(i)} for i in range(5)])

    result = await s.get_items()
    assert len(result) == 2
    assert result[0].get("content") == "3"
    assert result[1].get("content") == "4"


async def test_explicit_limit_overrides_session_settings() -> None:
    """An explicit limit passed to get_items must override session_settings.limit."""
    MongoDBSession._init_state.clear()
    s = MongoDBSession(
        "override-test",
        client=FakeAsyncMongoClient(),  # type: ignore[arg-type]
        database="agents_test",
        session_settings=SessionSettings(limit=10),
    )
    await s.add_items([{"role": "user", "content": str(i)} for i in range(8)])

    result = await s.get_items(limit=2)
    assert len(result) == 2
    assert result[0].get("content") == "6"
    assert result[1].get("content") == "7"


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


async def test_sessions_are_isolated() -> None:
    """Two sessions with different IDs must not share data."""
    MongoDBSession._init_state.clear()
    client = FakeAsyncMongoClient()
    s1 = MongoDBSession("alice", client=client, database="agents_test")  # type: ignore[arg-type]
    s2 = MongoDBSession("bob", client=client, database="agents_test")  # type: ignore[arg-type]

    await s1.add_items([{"role": "user", "content": "alice msg"}])
    await s2.add_items([{"role": "user", "content": "bob msg"}])

    assert [i.get("content") for i in await s1.get_items()] == ["alice msg"]
    assert [i.get("content") for i in await s2.get_items()] == ["bob msg"]


async def test_clear_does_not_affect_other_sessions() -> None:
    """Clearing one session must leave sibling sessions untouched."""
    MongoDBSession._init_state.clear()
    client = FakeAsyncMongoClient()
    s1 = MongoDBSession("s1", client=client, database="agents_test")  # type: ignore[arg-type]
    s2 = MongoDBSession("s2", client=client, database="agents_test")  # type: ignore[arg-type]

    await s1.add_items([{"role": "user", "content": "keep"}])
    await s2.add_items([{"role": "user", "content": "delete"}])

    await s2.clear_session()

    assert len(await s1.get_items()) == 1
    assert await s2.get_items() == []


# ---------------------------------------------------------------------------
# Serialisation / unicode safety
# ---------------------------------------------------------------------------


async def test_unicode_content_roundtrip(session: MongoDBSession) -> None:
    """Unicode and emoji content must survive the serialisation round-trip."""
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "😊👍"},
        {"role": "user", "content": "Привет"},
    ]
    await session.add_items(items)
    result = await session.get_items()
    assert result[0].get("content") == "こんにちは"
    assert result[1].get("content") == "😊👍"
    assert result[2].get("content") == "Привет"


async def test_json_special_characters(session: MongoDBSession) -> None:
    """Items containing JSON-special strings must be stored without corruption."""
    items: list[TResponseInputItem] = [
        {"role": "user", "content": '{"nested": "value"}'},
        {"role": "assistant", "content": 'Quote: "Hello"'},
        {"role": "user", "content": "Line1\nLine2\tTabbed"},
    ]
    await session.add_items(items)
    result = await session.get_items()
    assert result[0].get("content") == '{"nested": "value"}'
    assert result[1].get("content") == 'Quote: "Hello"'
    assert result[2].get("content") == "Line1\nLine2\tTabbed"


async def test_corrupted_document_is_skipped(session: MongoDBSession) -> None:
    """Documents with invalid JSON in message_data are silently skipped."""
    await session.add_items([{"role": "user", "content": "valid"}])

    # Inject a corrupted document directly into the fake collection.
    bad_doc = {
        "_id": FakeObjectId(),
        "session_id": session.session_id,
        "message_data": "not valid json {{{",
    }
    session._messages._docs[id(bad_doc["_id"])] = bad_doc

    items = await session.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "valid"


async def test_missing_message_data_field_is_skipped(session: MongoDBSession) -> None:
    """Documents without a message_data field are silently skipped."""
    await session.add_items([{"role": "user", "content": "valid"}])

    bad_doc = {"_id": FakeObjectId(), "session_id": session.session_id}
    session._messages._docs[id(bad_doc["_id"])] = bad_doc

    items = await session.get_items()
    assert len(items) == 1


async def test_non_string_message_data_is_skipped(session: MongoDBSession) -> None:
    """Documents whose message_data is a non-string BSON type are silently skipped."""
    await session.add_items([{"role": "user", "content": "valid"}])

    # Inject a document where message_data is an integer — json.loads raises TypeError.
    bad_doc = {"_id": FakeObjectId(), "session_id": session.session_id, "message_data": 42}
    session._messages._docs[id(bad_doc["_id"])] = bad_doc

    items = await session.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "valid"


async def test_pop_item_skips_corrupt_most_recent(session: MongoDBSession) -> None:
    """pop_item must skip a corrupt most-recent document and return the next valid one."""
    await session.add_items([{"role": "user", "content": "valid"}])

    # Inject a corrupt document with a higher seq so it sorts as "most recent".
    bad_doc = {
        "_id": FakeObjectId(),
        "session_id": session.session_id,
        "seq": 999,
        "message_data": "not valid json {{{",
    }
    session._messages._docs[id(bad_doc["_id"])] = bad_doc

    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "valid"

    # Both the corrupt doc and the valid one are now gone.
    assert await session.get_items() == []


async def test_pop_item_returns_none_when_only_corrupt_docs_remain(
    session: MongoDBSession,
) -> None:
    """pop_item must drop every corrupt doc and return None when nothing valid remains."""
    bad1 = {
        "_id": FakeObjectId(),
        "session_id": session.session_id,
        "seq": 1,
        "message_data": "garbage",
    }
    bad2 = {
        "_id": FakeObjectId(),
        "session_id": session.session_id,
        "seq": 2,
        "message_data": 42,  # non-string — TypeError
    }
    session._messages._docs[id(bad1["_id"])] = bad1
    session._messages._docs[id(bad2["_id"])] = bad2

    assert await session.pop_item() is None
    # Both corrupt docs must have been removed in the process.
    assert session._messages._docs == {}


# ---------------------------------------------------------------------------
# Index initialisation (idempotency)
# ---------------------------------------------------------------------------


async def test_index_creation_runs_only_once(session: MongoDBSession) -> None:
    """_ensure_indexes must call create_index only on the very first call."""
    call_count = 0
    original_messages = session._messages.create_index
    original_sessions = session._sessions.create_index

    async def counting(*args: Any, **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        return "fake_index"

    session._messages.create_index = counting  # type: ignore[method-assign]
    session._sessions.create_index = counting  # type: ignore[method-assign]

    await session._ensure_indexes()
    await session._ensure_indexes()  # Second call must be a no-op.

    # Exactly one call per collection (sessions + messages).
    assert call_count == 2

    session._messages.create_index = original_messages  # type: ignore[method-assign]
    session._sessions.create_index = original_sessions  # type: ignore[method-assign]


async def test_different_clients_each_run_index_init() -> None:
    """Each distinct AsyncMongoClient gets its own index-creation pass."""
    MongoDBSession._init_state.clear()

    client_a = FakeAsyncMongoClient()
    client_b = FakeAsyncMongoClient()

    call_counts: dict[str, int] = {"a": 0, "b": 0}

    async def counting_a(*args: Any, **kwargs: Any) -> str:
        call_counts["a"] += 1
        return "fake_index"

    async def counting_b(*args: Any, **kwargs: Any) -> str:
        call_counts["b"] += 1
        return "fake_index"

    s_a = MongoDBSession("x", client=client_a, database="agents_test")  # type: ignore[arg-type]
    s_b = MongoDBSession("x", client=client_b, database="agents_test")  # type: ignore[arg-type]

    s_a._messages.create_index = counting_a  # type: ignore[method-assign]
    s_a._sessions.create_index = counting_a  # type: ignore[method-assign]
    s_b._messages.create_index = counting_b  # type: ignore[method-assign]
    s_b._sessions.create_index = counting_b  # type: ignore[method-assign]

    await s_a._ensure_indexes()
    await s_b._ensure_indexes()

    # Each client must trigger its own index creation (2 calls = sessions + messages).
    assert call_counts["a"] == 2
    assert call_counts["b"] == 2


# ---------------------------------------------------------------------------
# Connectivity and lifecycle
# ---------------------------------------------------------------------------


async def test_ping_success(session: MongoDBSession) -> None:
    """ping() must return True when the client responds normally."""
    assert await session.ping() is True


async def test_ping_failure(session: MongoDBSession) -> None:
    """ping() must return False when the server raises an exception."""
    original = session._client.admin.command

    async def _fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("unreachable")

    session._client.admin.command = _fail  # type: ignore[method-assign, assignment]
    assert await session.ping() is False
    session._client.admin.command = original  # type: ignore[method-assign]


async def test_close_external_client_not_closed() -> None:
    """close() must NOT close a client that was injected externally."""
    MongoDBSession._init_state.clear()
    client = FakeAsyncMongoClient()
    s = MongoDBSession("x", client=client, database="agents_test")  # type: ignore[arg-type]
    assert s._owns_client is False

    await s.close()
    assert not client._closed


async def test_close_owned_client_is_closed() -> None:
    """close() must close a client created by from_uri."""
    MongoDBSession._init_state.clear()
    fake_client = FakeAsyncMongoClient()
    with patch(
        "agents.extensions.memory.mongodb_session.AsyncMongoClient",
        return_value=fake_client,
    ):
        s = MongoDBSession.from_uri("owned", uri="mongodb://localhost:27017", database="t")
        assert s._owns_client is True

        await s.close()
        assert fake_client._closed


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


async def test_runner_integration(agent: Agent) -> None:
    """MongoDBSession must supply conversation history to the Runner."""
    session = _make_session("runner-test")

    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("San Francisco")])
    result1 = await Runner.run(agent, "Where is the Golden Gate Bridge?", session=session)
    assert result1.final_output == "San Francisco"

    agent.model.set_next_output([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    last_input = agent.model.last_turn_args["input"]
    assert len(last_input) > 1
    assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)


async def test_runner_session_isolation(agent: Agent) -> None:
    """Two independent sessions must not bleed history into each other."""
    MongoDBSession._init_state.clear()
    client = FakeAsyncMongoClient()
    s1 = MongoDBSession("user-a", client=client, database="agents_test")  # type: ignore[arg-type]
    s2 = MongoDBSession("user-b", client=client, database="agents_test")  # type: ignore[arg-type]

    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("I like cats.")])
    await Runner.run(agent, "I like cats.", session=s1)

    agent.model.set_next_output([get_text_message("I like dogs.")])
    await Runner.run(agent, "I like dogs.", session=s2)

    agent.model.set_next_output([get_text_message("You said you like cats.")])
    result = await Runner.run(agent, "What animal did I mention?", session=s1)
    assert "cats" in result.final_output.lower()
    assert "dogs" not in result.final_output.lower()


async def test_runner_with_session_settings_limit(agent: Agent) -> None:
    """RunConfig.session_settings.limit must cap the history sent to the model."""
    from agents import RunConfig

    MongoDBSession._init_state.clear()
    session = MongoDBSession(
        "limit-test",
        client=FakeAsyncMongoClient(),  # type: ignore[arg-type]
        database="agents_test",
        session_settings=SessionSettings(limit=100),
    )

    history: list[TResponseInputItem] = [
        {"role": "user", "content": f"Turn {i}"} for i in range(10)
    ]
    await session.add_items(history)

    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("Got it")])
    await Runner.run(
        agent,
        "New question",
        session=session,
        run_config=RunConfig(session_settings=SessionSettings(limit=2)),
    )

    last_input = agent.model.last_turn_args["input"]
    history_items = [i for i in last_input if i.get("content") != "New question"]
    assert len(history_items) == 2


# ---------------------------------------------------------------------------
# Client metadata (driver handshake)
# ---------------------------------------------------------------------------


async def test_injected_client_receives_append_metadata() -> None:
    """Append_metadata is called on a caller-supplied client."""
    MongoDBSession._init_state.clear()
    client = FakeAsyncMongoClient()

    MongoDBSession("meta-test", client=client, database="agents_test")  # type: ignore[arg-type]

    assert len(client._metadata_calls) == 1
    info = client._metadata_calls[0]
    assert info.name == "openai-agents"


async def test_from_uri_passes_driver_info_to_constructor() -> None:
    """driver=_DRIVER_INFO is forwarded to AsyncMongoClient via from_uri."""
    MongoDBSession._init_state.clear()

    captured_kwargs: dict[str, Any] = {}

    def _fake_client(uri: str, **kwargs: Any) -> FakeAsyncMongoClient:
        captured_kwargs.update(kwargs)
        return FakeAsyncMongoClient()

    with patch(
        "agents.extensions.memory.mongodb_session.AsyncMongoClient",
        side_effect=_fake_client,
    ):
        MongoDBSession.from_uri("uri-test", uri="mongodb://localhost:27017", database="t")

    assert "driver" in captured_kwargs
    assert captured_kwargs["driver"].name == "openai-agents"


async def test_caller_supplied_driver_info_is_not_overwritten() -> None:
    """A caller-supplied driver kwarg must not be silently replaced."""
    MongoDBSession._init_state.clear()

    captured_kwargs: dict[str, Any] = {}
    custom_info = FakeDriverInfo(name="MyApp")

    def _fake_client(uri: str, **kwargs: Any) -> FakeAsyncMongoClient:
        captured_kwargs.update(kwargs)
        return FakeAsyncMongoClient()

    with patch(
        "agents.extensions.memory.mongodb_session.AsyncMongoClient",
        side_effect=_fake_client,
    ):
        MongoDBSession.from_uri(
            "uri-test",
            uri="mongodb://localhost:27017",
            database="t",
            client_kwargs={"driver": custom_info},
        )

    # The caller's value must be preserved — setdefault must not overwrite it.
    assert captured_kwargs["driver"] is custom_info
